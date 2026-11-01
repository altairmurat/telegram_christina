import ast
import base64
import os
import traceback
from typing import Literal
import requests
from pydantic import BaseModel, Field
from google import genai
from google.genai import types
from env import GEMINI_API_KEY

DEFAULT_REPO = "altairmurat/telegram_christina"
DEFAULT_BRANCH = "main"

CODE_EXTENSIONS = (".py", ".txt", ".yaml", ".yml", ".json", ".md", "Dockerfile")
IGNORED_PATHS = {
    ".env",
    "credentials.json",
    "token.json",
    "session_cookie.txt",
    "dgist_seen_ids.json",
    "test.db",
}


class CodePatch(BaseModel):
    target: str = Field(
        description="Exact existing code block from the file to be replaced. Include enough surrounding lines to be unique."
    )
    replacement: str = Field(
        description="Complete drop-in replacement code block with exact indentation."
    )


class FileModification(BaseModel):
    filepath: str = Field(
        description="Relative file path in the repository, e.g. 'main.py', 'llm.py', 'dgist_bot_handlers.py'."
    )
    operation: Literal["patch", "create", "overwrite"] = Field(
        description="Use 'patch' for modifying existing files via target/replacement blocks. Use 'create' for new files or 'overwrite' only when rewriting a small file completely."
    )
    patches: list[CodePatch] = Field(
        default_factory=list,
        description="List of search-and-replace patches when operation is 'patch'.",
    )
    full_content: str = Field(
        default="",
        description="Full file content only when operation is 'create' or 'overwrite'.",
    )


class SelfImprovementPlan(BaseModel):
    summary_ru: str = Field(
        description="Подробное и понятное объяснение на русском языке для владельца бота: что именно изменено, в каких файлах и как теперь это работает."
    )
    commit_message: str = Field(
        description="Short, clean git commit message in English describing the change."
    )
    modifications: list[FileModification] = Field(
        description="List of files to modify or create."
    )


def get_github_token() -> str | None:
    """Находит GitHub PAT в переменных окружения (поддерживает любые стандартные имена или префиксы ghp_ / github_pat_)."""
    candidate_keys = (
        "GITHUB_TOKEN",
        "GITHUB_PAT",
        "GH_TOKEN",
        "GITHUB_API_TOKEN",
        "GITHUB_API_KEY",
        "GIT_TOKEN",
        "PAT",
    )
    for k in candidate_keys:
        val = (os.getenv(k) or "").strip().strip('"').strip("'")
        if val:
            return val

    for k, val in os.environ.items():
        v = (val or "").strip().strip('"').strip("'")
        if v.startswith("ghp_") or v.startswith("github_pat_"):
            return v
    return None


def get_repo_info() -> tuple[str, str]:
    repo = (os.getenv("GITHUB_REPO") or DEFAULT_REPO).strip().strip("/")
    if repo.endswith(".git"):
        repo = repo[:-4]
    if "github.com/" in repo:
        repo = repo.split("github.com/", 1)[1].strip("/")
    branch = (os.getenv("GITHUB_BRANCH") or DEFAULT_BRANCH).strip()
    return repo, branch


def _gh_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def fetch_repo_snapshot(token: str, repo: str, branch: str) -> tuple[str, str, dict[str, str]]:
    """Возвращает (head_commit_sha, base_tree_sha, {filepath: content})."""
    headers = _gh_headers(token)
    ref_url = f"https://api.github.com/repos/{repo}/git/ref/heads/{branch}"
    r_ref = requests.get(ref_url, headers=headers, timeout=20)
    if r_ref.status_code != 200:
        raise RuntimeError(
            f"Ошибка доступа к GitHub репозиторию {repo} (ветка {branch}): HTTP {r_ref.status_code} — {r_ref.text[:300]}"
        )
    head_sha = r_ref.json()["object"]["sha"]

    commit_url = f"https://api.github.com/repos/{repo}/git/commits/{head_sha}"
    r_commit = requests.get(commit_url, headers=headers, timeout=20)
    r_commit.raise_for_status()
    base_tree_sha = r_commit.json()["tree"]["sha"]

    tree_url = f"https://api.github.com/repos/{repo}/git/trees/{base_tree_sha}?recursive=1"
    r_tree = requests.get(tree_url, headers=headers, timeout=25)
    r_tree.raise_for_status()
    tree_items = r_tree.json().get("tree", [])

    files: dict[str, str] = {}
    for item in tree_items:
        if item.get("type") != "blob":
            continue
        path = item.get("path", "")
        size = item.get("size", 0)
        if not path or path in IGNORED_PATHS or path.startswith(".") or "/" in path and path.split("/")[0] in ("user_data", "browser_profile", "__pycache__"):
            continue
        if not (path.endswith(CODE_EXTENSIONS) or path == "Dockerfile"):
            continue
        if size > 180_000:
            continue

        blob_url = f"https://api.github.com/repos/{repo}/contents/{path}?ref={head_sha}"
        rb = requests.get(blob_url, headers=headers, timeout=20)
        if rb.status_code == 200:
            data = rb.json()
            if data.get("encoding") == "base64" and data.get("content"):
                try:
                    files[path] = base64.b64decode(data["content"]).decode("utf-8")
                except Exception:
                    pass
    return head_sha, base_tree_sha, files


def _apply_single_patch(content: str, target: str, replacement: str) -> str | None:
    if not target:
        return None
    if target in content:
        return content.replace(target, replacement, 1)

    norm_content = content.replace("\r\n", "\n")
    norm_target = target.replace("\r\n", "\n")
    norm_replacement = replacement.replace("\r\n", "\n")
    if norm_target in norm_content:
        return norm_content.replace(norm_target, norm_replacement, 1)

    # Fallback: сопоставление по строкам без правых пробелов
    c_lines = norm_content.split("\n")
    t_lines = norm_target.strip("\n").split("\n")
    if not t_lines:
        return None
    t_stripped = [ln.rstrip() for ln in t_lines]
    n_t = len(t_stripped)
    for i in range(len(c_lines) - n_t + 1):
        window = [c_lines[i + j].rstrip() for j in range(n_t)]
        if window == t_stripped:
            new_lines = c_lines[:i] + norm_replacement.strip("\n").split("\n") + c_lines[i + n_t :]
            return "\n".join(new_lines)
    return None


def _validate_python_syntax(filepath: str, code: str) -> str | None:
    """Возвращает None если синтаксис корректен, иначе текст ошибки."""
    if not filepath.endswith(".py"):
        return None
    try:
        ast.parse(code, filename=filepath)
        compile(code, filepath, "exec")
        return None
    except SyntaxError as e:
        return f"SyntaxError в {filepath} (строка {e.lineno}): {e.msg}\nТекст строки: {e.text}"
    except Exception as e:
        return f"Ошибка компиляции {filepath}: {e}"


def _build_codebase_prompt(files: dict[str, str]) -> str:
    blocks = []
    # Приоритетный порядок файлов
    priority = [
        "main.py",
        "llm.py",
        "dgist_bot_handlers.py",
        "dgist_monitor.py",
        "fal_video.py",
        "google_calendar.py",
        "group_memory.py",
        "models.py",
        "database.py",
        "env.py",
        "requirements.txt",
    ]
    ordered_keys = [k for k in priority if k in files] + [k for k in sorted(files.keys()) if k not in priority]
    for path in ordered_keys:
        blocks.append(f"=== FILE: {path} ===\n{files[path]}\n=== END OF FILE: {path} ===")
    return "\n\n".join(blocks)


def _call_gemini_coder(system_instruction: str, prompt: str) -> SelfImprovementPlan:
    client = genai.Client(api_key=GEMINI_API_KEY)
    env_model = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
    models_to_try = []
    for m in ("gemini-3.8-flash", env_model, "gemini-3.5-flash-lite", "gemini-3.1-pro-preview"):
        if m and m not in models_to_try:
            models_to_try.append(m)
    last_err = None
    for model_name in models_to_try:
        try:
            resp = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    response_mime_type="application/json",
                    response_schema=SelfImprovementPlan,
                    temperature=0.1,
                ),
            )
            return SelfImprovementPlan.model_validate_json(resp.text)
        except Exception as e:
            last_err = e
            print(f"[AGY] Модель {model_name} вернула ошибку: {e}, пробую fallback...")
    raise RuntimeError(f"Не удалось сгенерировать изменения кода через Gemini: {last_err}")


def push_commit_to_github(
    token: str,
    repo: str,
    branch: str,
    base_commit_sha: str,
    base_tree_sha: str,
    modified_files: dict[str, str],
    commit_message: str,
) -> str:
    headers = _gh_headers(token)
    tree_elements = []
    for filepath, content in modified_files.items():
        tree_elements.append(
            {
                "path": filepath,
                "mode": "100644",
                "type": "blob",
                "content": content,
            }
        )

    r_new_tree = requests.post(
        f"https://api.github.com/repos/{repo}/git/trees",
        headers=headers,
        json={"base_tree": base_tree_sha, "tree": tree_elements},
        timeout=30,
    )
    if r_new_tree.status_code not in (200, 201):
        raise RuntimeError(f"Ошибка создания Git tree: {r_new_tree.status_code} {r_new_tree.text[:300]}")
    new_tree_sha = r_new_tree.json()["sha"]

    r_new_commit = requests.post(
        f"https://api.github.com/repos/{repo}/git/commits",
        headers=headers,
        json={
            "message": f"[Antigravity Auto] {commit_message}",
            "tree": new_tree_sha,
            "parents": [base_commit_sha],
        },
        timeout=30,
    )
    if r_new_commit.status_code not in (200, 201):
        raise RuntimeError(f"Ошибка создания Git commit: {r_new_commit.status_code} {r_new_commit.text[:300]}")
    new_commit_sha = r_new_commit.json()["sha"]

    r_update_ref = requests.patch(
        f"https://api.github.com/repos/{repo}/git/refs/heads/{branch}",
        headers=headers,
        json={"sha": new_commit_sha, "force": False},
        timeout=30,
    )
    if r_update_ref.status_code != 200:
        raise RuntimeError(f"Ошибка обновления ветки {branch}: {r_update_ref.status_code} {r_update_ref.text[:300]}")
    return new_commit_sha


def rollback_github_branch(target_commit_sha: str) -> dict:
    """Откатывает ветку на указанный SHA коммита (создавая новый коммит с деревом от старого коммита, без force-push)."""
    token = get_github_token()
    if not token:
        raise RuntimeError("Не найден GitHub токен в переменных окружения.")
    repo, branch = get_repo_info()
    headers = _gh_headers(token)

    # Получаем текущий HEAD
    r_ref = requests.get(f"https://api.github.com/repos/{repo}/git/ref/heads/{branch}", headers=headers, timeout=20)
    r_ref.raise_for_status()
    current_head_sha = r_ref.json()["object"]["sha"]

    # Получаем tree целевого коммита, к которому откатываемся
    r_target = requests.get(
        f"https://api.github.com/repos/{repo}/git/commits/{target_commit_sha}", headers=headers, timeout=20
    )
    r_target.raise_for_status()
    target_tree_sha = r_target.json()["tree"]["sha"]

    # Создаём коммит отката поверх текущего HEAD с деревом target_tree_sha
    r_revert_commit = requests.post(
        f"https://api.github.com/repos/{repo}/git/commits",
        headers=headers,
        json={
            "message": f"[Antigravity Rollback] Revert to {target_commit_sha[:7]}",
            "tree": target_tree_sha,
            "parents": [current_head_sha],
        },
        timeout=25,
    )
    r_revert_commit.raise_for_status()
    new_sha = r_revert_commit.json()["sha"]

    r_upd = requests.patch(
        f"https://api.github.com/repos/{repo}/git/refs/heads/{branch}",
        headers=headers,
        json={"sha": new_sha, "force": False},
        timeout=25,
    )
    r_upd.raise_for_status()
    return {
        "repo": repo,
        "branch": branch,
        "reverted_to": target_commit_sha,
        "new_commit_sha": new_sha,
    }


def execute_self_improvement_sync(user_instruction: str) -> dict:
    token = get_github_token()
    if not token:
        raise RuntimeError(
            "GitHub токен (PAT) не найден в окружении!\n"
            "Проверь, что на Render добавлена переменная `GITHUB_TOKEN` (или любой токен, начинающийся с `ghp_` / `github_pat_`)."
        )

    repo, branch = get_repo_info()
    head_sha, base_tree_sha, repo_files = fetch_repo_snapshot(token, repo, branch)
    if not repo_files:
        raise RuntimeError("Не удалось прочитать файлы репозитория из GitHub.")

    system_instruction = (
        "Ты — Antigravity, встроенный автономный ИИ-разработчик Telegram-бота «Кристина» (на базе Telethon, FastAPI, SQLAlchemy и Google Gemini SDK).\n"
        "Владелец бота прислал тебе задачу на самоулучшение / изменение кода бота.\n"
        "Твоя задача — аккуратно внести изменения в нужные файлы репозитория так, чтобы код гарантированно скомпилировался и не сломал существующий функционал.\n\n"
        "ПРАВИЛА:\n"
        "1. Для существующих файлов ВСЕГДА используй operation='patch' и указывай точные куски кода в `target` и `replacement` (с сохранением всех отступов!).\n"
        "2. НИКОГДА не удаляй и не ломай файл `agy_self_improve.py` или обработчик команды `/antigravity` / `/agy`.\n"
        "3. НИКОГДА не используй устаревший пакет `google.generativeai` — в проекте используется только новый `from google import genai`.\n"
        "4. Проверяй все импорты и отступы Python."
    )

    codebase_str = _build_codebase_prompt(repo_files)
    user_prompt = (
        f"ЗАДАЧА ОТ ВЛАДЕЛЬЦА БОТА:\n{user_instruction}\n\n"
        f"ТЕКУЩИЙ КОД ПРОЕКТА В GITHUB ({repo}@{branch}):\n{codebase_str}"
    )

    plan = _call_gemini_coder(system_instruction, user_prompt)
    if not plan.modifications:
        raise RuntimeError(f"Агент не предложил изменений в файлах.\nОтвет: {plan.summary_ru}")

    modified_files: dict[str, str] = {}
    patch_errors: list[str] = []

    for mod in plan.modifications:
        path = mod.filepath.strip().lstrip("/")
        if path in IGNORED_PATHS or path == "agy_self_improve.py":
            continue

        if mod.operation in ("create", "overwrite") or path not in repo_files:
            if not mod.full_content.strip():
                patch_errors.append(f"Пустой контент для файла {path}")
                continue
            new_content = mod.full_content.replace("\r\n", "\n")
        else:
            cur_content = modified_files.get(path, repo_files[path])
            for idx, p in enumerate(mod.patches):
                updated = _apply_single_patch(cur_content, p.target, p.replacement)
                if updated is None:
                    patch_errors.append(f"Не удалось найти блок target #{idx+1} в файле {path}")
                else:
                    cur_content = updated
            new_content = cur_content

        syn_err = _validate_python_syntax(path, new_content)
        if syn_err:
            patch_errors.append(syn_err)
        else:
            modified_files[path] = new_content

    # Если возникла ошибка применения патча или синтаксиса, даём Gemini 1 автоматическую попытку исправить
    if patch_errors:
        repair_prompt = (
            f"При применении твоих изменений возникли следующие ошибки:\n"
            + "\n".join(patch_errors)
            + f"\n\nИсходная задача владельца: {user_instruction}\n"
            "Исправь патчи так, чтобы `target` в точности совпадал с исходным файлом и синтаксис Python был на 100% валидным!\n\n"
            f"{codebase_str}"
        )
        plan = _call_gemini_coder(system_instruction, repair_prompt)
        modified_files.clear()
        final_errors = []
        for mod in plan.modifications:
            path = mod.filepath.strip().lstrip("/")
            if path in IGNORED_PATHS or path == "agy_self_improve.py":
                continue
            if mod.operation in ("create", "overwrite") or path not in repo_files:
                new_content = mod.full_content.replace("\r\n", "\n")
            else:
                cur_content = modified_files.get(path, repo_files[path])
                for idx, p in enumerate(mod.patches):
                    updated = _apply_single_patch(cur_content, p.target, p.replacement)
                    if updated is None:
                        final_errors.append(f"Не найден target #{idx+1} в {path}")
                    else:
                        cur_content = updated
                new_content = cur_content
            syn_err = _validate_python_syntax(path, new_content)
            if syn_err:
                final_errors.append(syn_err)
            else:
                modified_files[path] = new_content

        if final_errors:
            raise RuntimeError("Авто-проверка кода остановила пуш из-за ошибок:\n" + "\n".join(final_errors))

    if not modified_files:
        raise RuntimeError("Ни один файл не был изменён.")

    new_commit_sha = push_commit_to_github(
        token=token,
        repo=repo,
        branch=branch,
        base_commit_sha=head_sha,
        base_tree_sha=base_tree_sha,
        modified_files=modified_files,
        commit_message=plan.commit_message,
    )

    return {
        "repo": repo,
        "branch": branch,
        "previous_commit_sha": head_sha,
        "new_commit_sha": new_commit_sha,
        "commit_url": f"https://github.com/{repo}/commit/{new_commit_sha}",
        "modified_files": list(modified_files.keys()),
        "summary_ru": plan.summary_ru,
        "commit_message": plan.commit_message,
    }
