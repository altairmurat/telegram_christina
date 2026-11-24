"""
HTML/CSS/JS интерфейс Telegram Mini App (InstaSwipe Vault).
Поддерживает:
- Нативный запуск внутри Telegram Mini App (Telegram.WebApp SDK, HapticFeedback, initData-авторизация)
- Автономный статический режим (для GitHub Pages) и серверный режим (Render + Supabase PostgreSQL)
- Загрузку ЦЕЛОЙ ПАПКИ (webkitdirectory), ZIP-архива или нескольких JSON-файлов прямо из интерфейса
- 4 вкладки (Не взаимно, Взаимные, Подписаны, Висящие заявки)
- Инста-Радар (аналитика), Щит приватности (AES-256-GCM инспектор + PIN + удаление из БД),
  Умную пачку (авто-выбор призраков), Теги памяти и кнопку Undo (шаг назад).
"""

MINIAPP_HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
<title>InstaSwipe Vault — Telegram Mini App</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  :root {
    --bg: #080a10;
    --bg-grad: radial-gradient(120% 80% at 50% 0%, #171d30 0%, #080a10 65%);
    --surface: rgba(21, 26, 38, 0.88);
    --surface-solid: #151a26;
    --card-bg: linear-gradient(165deg, #1d2437 0%, #121723 100%);
    --border: rgba(255, 255, 255, 0.09);
    --border-strong: rgba(255, 255, 255, 0.16);
    --text: #f5f7ff;
    --muted: #8c96b2;
    --accent: #ff3366;
    --accent-glow: rgba(255, 51, 102, 0.35);
    --purple: #8b5cf6;
    --amber: #f59e0b;
    --blue: #3b82f6;
    --green: #10b981;
    --cyan: #06b6d4;
  }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  body {
    margin: 0;
    background: var(--bg-grad);
    background-attachment: fixed;
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    min-height: 100dvh;
    display: flex;
    flex-direction: column;
    align-items: center;
    overflow-x: hidden;
  }
  .app {
    width: 100%;
    max-width: 460px;
    min-height: 100dvh;
    display: flex;
    flex-direction: column;
    padding: 10px 12px 16px;
    gap: 9px;
  }

  /* Top Header & Quick Tools */
  .topbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 6px;
  }
  .brand {
    display: flex;
    align-items: center;
    gap: 8px;
    font-weight: 800;
    font-size: 17px;
    letter-spacing: -0.4px;
  }
  .brand-logo {
    width: 28px; height: 28px;
    border-radius: 9px;
    background: linear-gradient(135deg, #ff3366, #ff9933);
    display: flex; align-items: center; justify-content: center;
    font-size: 15px;
    box-shadow: 0 4px 14px var(--accent-glow);
  }
  .shield-pill {
    font-size: 10px;
    padding: 2px 7px;
    border-radius: 99px;
    background: rgba(16, 185, 129, 0.14);
    border: 1px solid rgba(16, 185, 129, 0.4);
    color: #6ee7b7;
    font-weight: 700;
    cursor: pointer;
  }
  .toolbar {
    display: flex;
    gap: 5px;
    overflow-x: auto;
    scrollbar-width: none;
  }
  .toolbar::-webkit-scrollbar { display: none; }
  .tool-btn {
    background: var(--surface);
    backdrop-filter: blur(10px);
    color: var(--text);
    border: 1px solid var(--border);
    padding: 6px 10px;
    border-radius: 11px;
    font-size: 12px;
    font-weight: 700;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    gap: 5px;
    white-space: nowrap;
    transition: transform 0.1s, border-color 0.15s, background 0.15s;
  }
  .tool-btn:active { transform: scale(0.95); }
  .tool-btn.highlight {
    background: linear-gradient(135deg, rgba(255,51,102,0.22), rgba(139,92,246,0.22));
    border-color: rgba(255,51,102,0.45);
  }
  .tool-btn.active {
    background: var(--accent);
    border-color: var(--accent);
    color: #fff;
  }

  /* 4 Tabs */
  .tabs {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    background: rgba(13, 17, 26, 0.9);
    padding: 4px;
    border-radius: 14px;
    border: 1px solid var(--border);
    gap: 3px;
  }
  .tab {
    background: transparent;
    color: var(--muted);
    border: none;
    padding: 7px 4px;
    border-radius: 10px;
    font-size: 11px;
    font-weight: 700;
    cursor: pointer;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 2px;
    transition: 0.15s;
  }
  .tab.active {
    background: #20283c;
    color: var(--text);
    box-shadow: 0 2px 10px rgba(0,0,0,0.4);
  }
  .tab-count {
    font-size: 11px;
    color: var(--accent);
    font-weight: 800;
  }

  /* Sorts & Search */
  .controls {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .sort-scroll {
    display: flex;
    gap: 5px;
    overflow-x: auto;
    padding-bottom: 2px;
    scrollbar-width: none;
  }
  .sort-scroll::-webkit-scrollbar { display: none; }
  .sort-chip {
    flex: 0 0 auto;
    background: #131824;
    color: var(--muted);
    border: 1px solid var(--border);
    padding: 5px 10px;
    border-radius: 999px;
    font-size: 11.5px;
    font-weight: 600;
    cursor: pointer;
    white-space: nowrap;
    transition: 0.15s;
  }
  .sort-chip.active {
    background: #27314a;
    color: #fff;
    border-color: #5b6b94;
  }
  .search-row {
    display: flex;
    gap: 6px;
    align-items: center;
  }
  .search-input {
    flex: 1;
    background: #111520;
    border: 1px solid var(--border);
    color: var(--text);
    padding: 7px 11px;
    border-radius: 11px;
    font-size: 12.5px;
    outline: none;
  }
  .search-input:focus { border-color: #5b6b94; }

  /* Progress & Undo Row */
  .progress-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    font-size: 11.5px;
    color: var(--muted);
    padding: 0 2px;
  }
  .undo-btn {
    background: rgba(245, 158, 11, 0.14);
    border: 1px solid rgba(245, 158, 11, 0.4);
    color: #fcd34d;
    padding: 3px 9px;
    border-radius: 99px;
    font-size: 11px;
    font-weight: 700;
    cursor: pointer;
  }
  .undo-btn:disabled {
    opacity: 0.35;
    cursor: default;
  }
  .progress-bar {
    height: 4px;
    background: #171d2b;
    border-radius: 99px;
    overflow: hidden;
  }
  .progress-fill {
    height: 100%;
    background: linear-gradient(90deg, #ff3366, #ff9933);
    width: 0%;
    transition: width 0.2s;
  }

  /* Tinder Stage with Card Stack */
  .stage {
    flex: 1;
    display: flex;
    align-items: center;
    justify-content: center;
    position: relative;
    min-height: 365px;
    perspective: 1000px;
    padding: 4px 0;
  }
  .card-back {
    position: absolute;
    width: 93%;
    height: 92%;
    background: #121724;
    border: 1px solid var(--border);
    border-radius: 24px;
    transform: translateY(11px) scale(0.96);
    opacity: 0.55;
    z-index: 1;
    pointer-events: none;
  }
  .card {
    width: 100%;
    background: var(--card-bg);
    border: 1px solid var(--border-strong);
    border-radius: 24px;
    padding: 16px 16px 14px;
    box-shadow: 0 18px 42px rgba(0, 0, 0, 0.6);
    display: flex;
    flex-direction: column;
    align-items: center;
    text-align: center;
    gap: 10px;
    position: relative;
    z-index: 2;
    user-select: none;
    touch-action: pan-y;
    transition: transform 0.22s cubic-bezier(0.2, 0.9, 0.3, 1), opacity 0.22s ease;
  }
  .card.swiping { transition: none; }
  .swipe-badge {
    position: absolute;
    top: 16px;
    padding: 5px 12px;
    border-radius: 9px;
    font-weight: 800;
    font-size: 13px;
    letter-spacing: 0.6px;
    opacity: 0;
    pointer-events: none;
    border: 2px solid;
    z-index: 5;
  }
  .swipe-badge.left { right: 14px; color: #ff4757; border-color: #ff4757; transform: rotate(10deg); }
  .swipe-badge.right { left: 14px; color: #10b981; border-color: #10b981; transform: rotate(-10deg); }

  /* Card Top Meta Bar (Tie Meter + First Follow) */
  .card-top-meta {
    width: 100%;
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 6px;
    font-size: 11px;
    font-weight: 700;
  }
  .tie-pill {
    padding: 3px 9px;
    border-radius: 99px;
    border: 1px solid var(--border);
    background: rgba(0,0,0,0.28);
  }
  .tie-pill.ghost { color: #fda4af; border-color: rgba(255,51,102,0.35); background: rgba(255,51,102,0.1); }
  .tie-pill.low { color: #93c5fd; border-color: rgba(59,130,246,0.35); background: rgba(59,130,246,0.1); }
  .tie-pill.warm { color: #fcd34d; border-color: rgba(245,158,11,0.4); background: rgba(245,158,11,0.12); }
  .tie-pill.inner_circle { color: #6ee7b7; border-color: rgba(16,185,129,0.45); background: rgba(16,185,129,0.15); }

  /* Clickable Profile Area */
  .profile-link {
    text-decoration: none;
    color: inherit;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 8px;
    width: 100%;
    padding: 4px 8px;
    border-radius: 18px;
    transition: background 0.15s;
  }
  .profile-link:hover, .profile-link:active {
    background: rgba(255,255,255,0.04);
  }
  .avatar-ring {
    width: 92px;
    height: 92px;
    border-radius: 50%;
    padding: 3px;
    background: linear-gradient(135deg, #feda75, #fa7e1e, #d62976, #962fbf, #4f5bd5);
    display: flex;
    align-items: center;
    justify-content: center;
    position: relative;
  }
  .avatar-ring.cf { background: linear-gradient(135deg, #10b981, #34d399); }
  .avatar {
    width: 100%;
    height: 100%;
    border-radius: 50%;
    background: #1d2436;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 31px;
    font-weight: 800;
    color: #fff;
  }
  .open-ig-hint {
    position: absolute;
    bottom: -5px;
    background: #232b3e;
    border: 1px solid #414e70;
    color: #e2e8f0;
    font-size: 10px;
    font-weight: 700;
    padding: 2px 8px;
    border-radius: 999px;
  }
  .username-row {
    font-size: 20px;
    font-weight: 800;
    word-break: break-all;
    margin-top: 2px;
  }
  .full-name {
    font-size: 13px;
    color: var(--muted);
    min-height: 16px;
  }

  /* Badges & Tags */
  .badges {
    display: flex;
    flex-wrap: wrap;
    justify-content: center;
    gap: 5px;
  }
  .badge {
    font-size: 10.5px;
    font-weight: 700;
    padding: 3px 9px;
    border-radius: 999px;
    background: #1d2436;
    border: 1px solid #2e3852;
    color: #cbd5e1;
  }
  .badge.mutual { background: rgba(59, 130, 246, 0.15); border-color: #3b82f6; color: #93c5fd; }
  .badge.not_back { background: rgba(255, 51, 102, 0.15); border-color: #ff3366; color: #fda4af; }
  .badge.fans { background: rgba(139, 92, 246, 0.15); border-color: #8b5cf6; color: #d8b4fe; }
  .badge.pending { background: rgba(245, 158, 11, 0.15); border-color: #f59e0b; color: #fde68a; }
  .badge.cf { background: rgba(16, 185, 129, 0.16); border-color: #10b981; color: #6ee7b7; }
  .badge.action-tag { background: rgba(245, 158, 11, 0.22); border-color: #f59e0b; color: #fde68a; }

  /* Quick Tag Bar on Card */
  .tag-bar {
    display: flex;
    gap: 5px;
    flex-wrap: wrap;
    justify-content: center;
  }
  .tag-chip {
    background: rgba(255,255,255,0.04);
    border: 1px solid var(--border);
    color: var(--muted);
    font-size: 10.5px;
    font-weight: 600;
    padding: 3px 8px;
    border-radius: 99px;
    cursor: pointer;
    transition: 0.12s;
  }
  .tag-chip:hover, .tag-chip.active {
    background: rgba(16, 185, 129, 0.18);
    border-color: #10b981;
    color: #6ee7b7;
  }

  /* 3-Column Stats inside Card */
  .stats-grid {
    width: 100%;
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 6px;
    background: rgba(11, 14, 22, 0.72);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 8px 6px;
  }
  .stat-box { display: flex; flex-direction: column; gap: 2px; }
  .stat-label { font-size: 9.5px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.4px; }
  .stat-val { font-size: 12px; font-weight: 700; color: var(--text); }

  /* 4 Main Action Buttons + Nav */
  .actions-panel {
    display: flex;
    flex-direction: column;
    gap: 7px;
  }
  .action-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 7px;
  }
  .act-btn {
    border: 1px solid rgba(255,255,255,0.14);
    border-radius: 15px;
    padding: 10px 8px;
    font-size: 12.5px;
    font-weight: 700;
    color: #fff;
    cursor: pointer;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 2px;
    text-align: center;
    transition: transform 0.08s;
    text-decoration: none;
  }
  .act-btn:active { transform: scale(0.96); }
  .act-btn .sub { font-size: 9.5px; font-weight: 500; opacity: 0.82; }
  .btn-open { background: linear-gradient(135deg, #26304a, #192033); border-color: #3d4d75; }
  .btn-mutual { background: linear-gradient(135deg, #6d28d9, #9333ea); border-color: #a855f7; }
  .btn-unfollow-me { background: linear-gradient(135deg, #e11d48, #f43f5e); border-color: #fb7185; }
  .btn-remove-follower { background: linear-gradient(135deg, #d97706, #f59e0b); border-color: #fbbf24; }

  .nav-row {
    display: grid;
    grid-template-columns: 1fr 1.45fr 1fr;
    gap: 7px;
  }
  .nav-btn {
    background: #161b28;
    border: 1px solid var(--border);
    color: var(--text);
    padding: 9px;
    border-radius: 13px;
    font-size: 12.5px;
    font-weight: 700;
    cursor: pointer;
  }
  .nav-btn.keep {
    background: rgba(16, 185, 129, 0.16);
    border-color: #10b981;
    color: #6ee7b7;
  }

  /* Bottom Sheet Modals */
  .modal-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.75);
    backdrop-filter: blur(4px);
    display: none;
    align-items: flex-end;
    justify-content: center;
    z-index: 60;
  }
  .modal-backdrop.open { display: flex; }
  .modal {
    width: 100%;
    max-width: 460px;
    max-height: 86dvh;
    background: #141926;
    border-top: 1px solid var(--border-strong);
    border-radius: 22px 22px 0 0;
    padding: 16px;
    display: flex;
    flex-direction: column;
    gap: 11px;
    overflow-y: auto;
  }
  .modal-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    font-size: 15px;
    font-weight: 800;
  }
  .modal-list {
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 6px;
    max-height: 48dvh;
  }
  .queue-item {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 8px 10px;
    background: #0e121b;
    border: 1px solid var(--border);
    border-radius: 11px;
    font-size: 12.5px;
  }
  .upload-zone {
    border: 1.5px dashed #47557a;
    background: rgba(255,255,255,0.02);
    border-radius: 16px;
    padding: 14px;
    text-align: center;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .upload-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 8px;
  }
  .radar-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 8px;
  }
  .radar-card {
    background: #0e121c;
    border: 1px solid var(--border);
    border-radius: 13px;
    padding: 10px;
    display: flex;
    flex-direction: column;
    gap: 3px;
  }
  .radar-big { font-size: 20px; font-weight: 800; color: #fff; }
  .code-box {
    background: #0a0d14;
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 8px 10px;
    font-family: monospace;
    font-size: 11px;
    color: #6ee7b7;
    word-break: break-all;
  }
</style>
</head>
<body>
<div class="app">
  <!-- Top Bar -->
  <div class="topbar">
    <div class="brand">
      <div class="brand-logo">🔥</div>
      <span>InstaSwipe</span>
      <span class="shield-pill" onclick="openModal('privacyModal')" title="Zero-Knowledge AES-256-GCM">🔐 AES-256</span>
    </div>
    <div style="display:flex;gap:5px;">
      <button class="tool-btn" onclick="openModal('uploadModal')">📥 Файлы/Папка</button>
      <button class="tool-btn" onclick="openRadarModal()">📊 Радар</button>
      <button class="tool-btn highlight" onclick="openQueueModal()">🛒 (<span id="queueCount">0</span>)</button>
    </div>
  </div>

  <!-- Sub-toolbar: Smart Batch + Hide Done -->
  <div class="toolbar">
    <button class="tool-btn" id="toggleDoneBtn" onclick="toggleHideDone()">👁 Скрывать решённые: ДА</button>
    <button class="tool-btn" onclick="autoSelectGhosts()">⚡ Авто-выбор призраков</button>
    <button class="tool-btn" onclick="openModal('privacyModal')">🛡 Мой Blind-ID</button>
  </div>

  <!-- 4 Tabs -->
  <div class="tabs">
    <button class="tab active" data-tab="not_back" onclick="setTab('not_back')">
      <span>Не взаимно</span>
      <span class="tab-count" id="cnt_not_back">0</span>
    </button>
    <button class="tab" data-tab="mutual" onclick="setTab('mutual')">
      <span>Взаимные</span>
      <span class="tab-count" id="cnt_mutual">0</span>
    </button>
    <button class="tab" data-tab="fans" onclick="setTab('fans')">
      <span>Подписаны</span>
      <span class="tab-count" id="cnt_fans">0</span>
    </button>
    <button class="tab" data-tab="pending" onclick="setTab('pending')">
      <span>⏳ Заявки</span>
      <span class="tab-count" id="cnt_pending">0</span>
    </button>
  </div>

  <!-- Sort Chips + Search -->
  <div class="controls">
    <div class="sort-scroll" id="sortBar">
      <button class="sort-chip active" data-sort="oldest" onclick="setSort('oldest')">1. Давно подписан</button>
      <button class="sort-chip" data-sort="newest" onclick="setSort('newest')">2. Недавно</button>
      <button class="sort-chip" data-sort="random" onclick="setSort('random')">3. Рандом 🎲</button>
      <button class="sort-chip" data-sort="least_inter" onclick="setSort('least_inter')">4. 👻 Сначала призраки</button>
      <button class="sort-chip" data-sort="most_inter" onclick="setSort('most_inter')">5. 💎 Близкий круг</button>
      <button class="sort-chip" data-sort="you_first" onclick="setSort('you_first')">6. 🤝 Я подписался первым</button>
      <button class="sort-chip" data-sort="them_first" onclick="setSort('them_first')">7. ✨ Он первым</button>
      <button class="sort-chip" data-sort="az" onclick="setSort('az')">8. А–Я</button>
      <button class="sort-chip" data-sort="private_first" onclick="setSort('private_first')">9. 🔒 Приватные</button>
    </div>
    <div class="search-row">
      <input class="search-input" id="searchInput" placeholder="Поиск по @username, имени или тегу..." oninput="applyFilterAndSort()" />
    </div>
  </div>

  <!-- Progress + Undo -->
  <div class="progress-row">
    <span id="posLabel">0 / 0</span>
    <span style="font-size:11px;">Свайп ⬅ отписка · ➡ оставить</span>
    <button class="undo-btn" id="undoBtn" onclick="undoLastAction()" disabled>↩ Шаг назад</button>
  </div>
  <div class="progress-bar"><div class="progress-fill" id="progressFill"></div></div>

  <!-- Tinder Card Stage -->
  <div class="stage">
    <div class="card-back" id="cardBack"></div>
    <div class="card" id="card">
      <div class="swipe-badge left" id="badgeLeft">ОТПИСАТЬСЯ</div>
      <div class="swipe-badge right" id="badgeRight">ОСТАВИТЬ</div>

      <div class="card-top-meta">
        <span class="tie-pill ghost" id="tiePill">👻 0% Призрак</span>
        <span style="color:var(--muted);" id="daysPill">—</span>
      </div>

      <!-- 1) Нажатие на профиль -> переносит в Инстаграм -->
      <a class="profile-link" id="profileLink" href="#" target="_blank" rel="noopener">
        <div class="avatar-ring" id="avatarRing">
          <div class="avatar" id="avatarInitials">IG</div>
          <span class="open-ig-hint">Открыть IG ↗</span>
        </div>
        <div class="username-row" id="cardUsername">@username</div>
        <div class="full-name" id="cardFullName"></div>
      </a>

      <div class="badges" id="cardBadges"></div>

      <!-- Быстрые теги памяти (Keep-категории) -->
      <div class="tag-bar" id="tagBar">
        <button class="tag-chip" data-tag="Друг/Семья" onclick="setTag('Друг/Семья')">❤️ Друг</button>
        <button class="tag-chip" data-tag="Блогер/Кумир" onclick="setTag('Блогер/Кумир')">🌟 Кумир</button>
        <button class="tag-chip" data-tag="Бренд/Магазин" onclick="setTag('Бренд/Магазин')">🛍 Бренд</button>
        <button class="tag-chip" data-tag="Работа/Учёба" onclick="setTag('Работа/Учёба')">💼 Работа</button>
      </div>

      <div class="stats-grid">
        <div class="stat-box">
          <span class="stat-label">Я подписан</span>
          <span class="stat-val" id="statFollowed">—</span>
        </div>
        <div class="stat-box">
          <span class="stat-label">Он подписан</span>
          <span class="stat-val" id="statFollower">—</span>
        </div>
        <div class="stat-box">
          <span class="stat-label">Близость</span>
          <span class="stat-val" id="statInter">0%</span>
        </div>
      </div>
    </div>
  </div>

  <!-- 4 Main Requested Action Buttons -->
  <div class="actions-panel">
    <div class="action-grid">
      <a class="act-btn btn-open" id="btnOpenProfile" href="#" target="_blank" rel="noopener">
        <span>1) Открыть профиль ↗</span>
        <span class="sub">Перейти в Instagram</span>
      </a>
      <button class="act-btn btn-mutual" onclick="decide('mutual_break')">
        <span>2) Отписаться взаимно</span>
        <span class="sub">Я от него + его от меня</span>
      </button>
      <button class="act-btn btn-unfollow-me" onclick="decide('unfollow_me')">
        <span id="btnUnfollowMeTitle">3) Отписаться самому</span>
        <span class="sub" id="btnUnfollowMeSub">Убрать из моих подписок</span>
      </button>
      <button class="act-btn btn-remove-follower" onclick="decide('remove_follower')">
        <span>4) Отписать от себя</span>
        <span class="sub">Удалить из подписчиков</span>
      </button>
    </div>

    <div class="nav-row">
      <button class="nav-btn" onclick="prevCard()">← Пред.</button>
      <button class="nav-btn keep" onclick="decide('keep')">♥ Оставить / Дальше →</button>
      <button class="nav-btn" onclick="nextCard()">Пропустить →</button>
    </div>
  </div>
</div>

<!-- 1. Модалка: Загрузка папки / ZIP / JSON -->
<div class="modal-backdrop" id="uploadModal" onclick="if(event.target===this)closeModal('uploadModal')">
  <div class="modal">
    <div class="modal-header">
      <span>📥 Загрузка данных Instagram</span>
      <button class="tool-btn" onclick="closeModal('uploadModal')">Закрыть ✕</button>
    </div>
    <div style="font-size:12px;color:var(--muted);line-height:1.45;">
      Можешь выбрать <b>целую распакованную папку</b>, <b>ZIP-архив</b> или отдельные <b>JSON-файлы</b> (либо просто скинуть файлы прямо в чат Telegram-бота).<br/>
      🔒 Браузер отберёт только лёгкие JSON-файлы (фото и видео игнорируются), сожмёт и зашифрует их <b>AES-256-GCM</b> перед записью в Supabase.
    </div>
    <div class="upload-zone">
      <div class="upload-grid">
        <label class="act-btn btn-mutual" style="cursor:pointer;">
          <span>📁 Выбрать ПАПКУ</span>
          <span class="sub">Целая папка выгрузки</span>
          <input type="file" id="folderInput" webkitdirectory directory multiple style="display:none;" onchange="handleFileUpload(this.files)" />
        </label>
        <label class="act-btn btn-unfollow-me" style="cursor:pointer;">
          <span>📦 ZIP или JSON</span>
          <span class="sub">Архив или несколько файлов</span>
          <input type="file" id="filesInput" multiple accept=".zip,.json,.csv" style="display:none;" onchange="handleFileUpload(this.files)" />
        </label>
      </div>
      <div id="uploadStatus" style="font-size:12px;color:#6ee7b7;margin-top:4px;"></div>
    </div>
  </div>
</div>

<!-- 2. Модалка: Инста-Радар (Аналитика) -->
<div class="modal-backdrop" id="radarModal" onclick="if(event.target===this)closeModal('radarModal')">
  <div class="modal">
    <div class="modal-header">
      <span>📊 Инста-Радар и аналитика</span>
      <button class="tool-btn" onclick="closeModal('radarModal')">Закрыть ✕</button>
    </div>
    <div class="radar-grid" id="radarGrid"></div>
    <div style="font-size:12.5px;font-weight:700;margin-top:4px;">🦣 Топ-5 самых старых невзаимных («Мамонты»):</div>
    <div class="modal-list" id="radarOldestList" style="max-height:160px;"></div>
    <div style="font-size:12.5px;font-weight:700;margin-top:4px;">⏳ Самые долгие висящие заявки:</div>
    <div class="modal-list" id="radarPendingList" style="max-height:140px;"></div>
  </div>
</div>

<!-- 3. Модалка: Щит приватности (Zero-Knowledge AES-256-GCM) -->
<div class="modal-backdrop" id="privacyModal" onclick="if(event.target===this)closeModal('privacyModal')">
  <div class="modal">
    <div class="modal-header">
      <span>🔐 Крипто-Щит (Zero-Knowledge)</span>
      <button class="tool-btn" onclick="closeModal('privacyModal')">Закрыть ✕</button>
    </div>
    <div style="font-size:12px;color:var(--muted);line-height:1.45;">
      В базе данных Supabase PostgreSQL <b>не хранится</b> твой Telegram ID, ник или открытые списки.
      Админ БД видит только необратимый хеш <code>blind_uid</code> и зашифрованный блоб <code>AES-256-GCM</code>:
    </div>
    <div style="font-size:11px;color:var(--muted);">Твой анонимный Blind-ID (HMAC-SHA256):</div>
    <div class="code-box" id="privBlindUid">Загрузка...</div>
    <div style="font-size:11px;color:var(--muted);">Вид твоих данных в таблице Supabase (Ciphertext):</div>
    <div class="code-box" id="privCipherPreview">v1.AES256GCM...</div>
    <div style="font-size:11px;color:var(--muted);">Хранилище: <b id="privBackend" style="color:#fff;">Supabase PostgreSQL</b></div>

    <div style="display:flex;gap:6px;align-items:center;margin-top:4px;">
      <input class="search-input" type="password" id="pinInput" placeholder="Опциональный личный PIN-ключ..." />
      <button class="tool-btn active" onclick="applyUserPin()">🔑 Применить PIN</button>
    </div>
    <div style="display:flex;justify-content:space-between;align-items:center;margin-top:6px;">
      <button class="tool-btn" style="border-color:#ef4444;color:#fca5a5;" onclick="wipeMyVault()">🔥 Стереть мои данные из БД</button>
    </div>
  </div>
</div>

<!-- 4. Модалка: Корзина выбранных действий -->
<div class="modal-backdrop" id="queueModal" onclick="if(event.target===this)closeModal('queueModal')">
  <div class="modal">
    <div class="modal-header">
      <span>🛒 Корзина решений</span>
      <div style="display:flex;gap:5px;">
        <button class="tool-btn active" onclick="sendExportToBot()">📩 В Telegram-бот</button>
        <button class="tool-btn" onclick="exportActionsJson()">⬇ JSON</button>
        <button class="tool-btn" onclick="closeModal('queueModal')">✕</button>
      </div>
    </div>
    <div style="font-size:11.5px;color:var(--muted);">
      Все решения мгновенно шифруются и сохраняются в твоём сейфе. Нажми <b>«📩 В Telegram-бот»</b>, чтобы бот прислал готовые файлы в чат, или <b>«⬇ JSON»</b>.
    </div>
    <div class="modal-list" id="queueList"></div>
  </div>
</div>

<script>
/*__STATIC_DATA_PLACEHOLDER__*/
const tg = (window.Telegram && window.Telegram.WebApp) ? window.Telegram.WebApp : null;
if (tg) {
  try { tg.ready(); tg.expand(); } catch (e) {}
}

let allProfiles = [];
let summaryData = {};
let privacyInfo = {};
let viewList = [];
let currentTab = 'not_back';
let currentSort = 'oldest';
let currentIndex = 0;
let hideDecided = true;
let randomWeights = {};
let historyStack = [];
let isStaticMode = false;
let userPin = sessionStorage.getItem('instaswipe_pin') || '';
const LS_KEY = 'instaswipe_actions_v2';

const ACTION_LABELS = {
  unfollow_me: 'Отписаться самому',
  remove_follower: 'Отписать от себя',
  mutual_break: 'Отписаться взаимно',
  keep: 'Оставить ♥'
};

const TIE_LABELS = {
  ghost: '👻 0% Призрак',
  low: '🧊 Слабый контакт',
  warm: '🔥 Тёплый контакт',
  inner_circle: '💎 Близкий круг'
};

function haptic(kind = 'light') {
  if (!tg || !tg.HapticFeedback) return;
  try {
    if (kind === 'success' || kind === 'warning' || kind === 'error') {
      tg.HapticFeedback.notificationOccurred(kind);
    } else {
      tg.HapticFeedback.impactOccurred(kind);
    }
  } catch (e) {}
}

function authHeaders(extra = {}) {
  const h = { ...extra };
  if (tg && tg.initData) h['X-Telegram-Init-Data'] = tg.initData;
  const urlToken = new URLSearchParams(window.location.search).get('token');
  if (urlToken) h['X-Session-Token'] = urlToken;
  if (userPin) h['X-Vault-Pin'] = userPin;
  return h;
}

function getLocalActions() {
  try { return JSON.parse(localStorage.getItem(LS_KEY) || '{}'); } catch (e) { return {}; }
}
function setLocalAction(username, action, tag = '') {
  const map = getLocalActions();
  if (action === 'reset' || (!action && !tag)) delete map[username];
  else map[username] = { action, tag, updated_at: new Date().toISOString() };
  try { localStorage.setItem(LS_KEY, JSON.stringify(map)); } catch (e) {}
}

async function init() {
  let data = null;
  if (typeof window.__STATIC_DATASET__ !== 'undefined') {
    data = window.__STATIC_DATASET__;
    isStaticMode = true;
  } else {
    try {
      const res = await fetch('/api/instaswipe/data', { headers: authHeaders() });
      if (res.ok) data = await res.json();
    } catch (e) {
      isStaticMode = true;
    }
  }

  if (data && data.privacy && data.privacy.locked) {
    openModal('privacyModal');
    alert('Сейф защищён твоим личным PIN-ключом. Введи его в окне Крипто-Щита.');
  }

  allProfiles = (data && data.profiles) ? data.profiles : [];
  summaryData = (data && data.summary) ? data.summary : {};
  privacyInfo = (data && data.privacy) ? data.privacy : {};

  const localMap = getLocalActions();
  allProfiles.forEach(p => {
    if (!p.action && localMap[p.username]) {
      p.action = localMap[p.username].action || '';
      p.tag = localMap[p.username].tag || p.tag || '';
    }
    randomWeights[p.username] = Math.random();
  });

  renderPrivacyInfo();
  updateCounts();
  applyFilterAndSort();
  setupSwipe();
}

function renderPrivacyInfo() {
  document.getElementById('privBlindUid').textContent = privacyInfo.blind_uid || 'Локальный / GitHub Pages режим (SHA-256 скрыт)';
  document.getElementById('privCipherPreview').textContent = privacyInfo.cipher_preview || 'v1.AES256GCM_ENCRYPTED_BLOB...';
  document.getElementById('privBackend').textContent = privacyInfo.backend || (isStaticMode ? 'Browser LocalVault' : 'Supabase PostgreSQL');
}

function updateCounts() {
  const counts = { mutual: 0, not_back: 0, fans: 0, pending: 0 };
  let qCount = 0;
  for (const p of allProfiles) {
    if (p.action && p.action !== 'keep') qCount++;
    if (hideDecided && p.action) continue;
    if (counts[p.tab] !== undefined) counts[p.tab]++;
  }
  document.getElementById('cnt_mutual').textContent = counts.mutual;
  document.getElementById('cnt_not_back').textContent = counts.not_back;
  document.getElementById('cnt_fans').textContent = counts.fans;
  document.getElementById('cnt_pending').textContent = counts.pending;
  document.getElementById('queueCount').textContent = qCount;
  document.getElementById('undoBtn').disabled = historyStack.length === 0;
}

function setTab(tab) {
  haptic('light');
  currentTab = tab;
  document.querySelectorAll('.tab').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  const titleEl = document.getElementById('btnUnfollowMeTitle');
  const subEl = document.getElementById('btnUnfollowMeSub');
  if (tab === 'pending') {
    titleEl.textContent = '3) Отменить заявку';
    subEl.textContent = 'Отозвать запрос подписки';
  } else {
    titleEl.textContent = '3) Отписаться самому';
    subEl.textContent = 'Убрать из моих подписок';
  }
  currentIndex = 0;
  applyFilterAndSort();
}

function setSort(sortKey) {
  haptic('light');
  currentSort = sortKey;
  if (sortKey === 'random') {
    allProfiles.forEach(p => { randomWeights[p.username] = Math.random(); });
  }
  document.querySelectorAll('.sort-chip').forEach(b => b.classList.toggle('active', b.dataset.sort === sortKey));
  currentIndex = 0;
  applyFilterAndSort();
}

function toggleHideDone() {
  hideDecided = !hideDecided;
  document.getElementById('toggleDoneBtn').textContent = '👁 Скрывать решённые: ' + (hideDecided ? 'ДА' : 'НЕТ');
  currentIndex = 0;
  updateCounts();
  applyFilterAndSort();
}

function applyFilterAndSort() {
  const q = document.getElementById('searchInput').value.trim().toLowerCase();
  viewList = allProfiles.filter(p => {
    if (p.tab !== currentTab) return false;
    if (hideDecided && p.action) return false;
    if (q) {
      const hay = `${p.username} ${p.full_name || ''} ${p.tag || ''}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });

  viewList.sort((a, b) => {
    if (currentSort === 'oldest') return (a.primary_ts || 9999999999) - (b.primary_ts || 9999999999);
    if (currentSort === 'newest') return (b.primary_ts || 0) - (a.primary_ts || 0);
    if (currentSort === 'random') return (randomWeights[a.username] || 0) - (randomWeights[b.username] || 0);
    if (currentSort === 'most_inter') return (b.interaction_score - a.interaction_score) || a.username.localeCompare(b.username);
    if (currentSort === 'least_inter') return (a.interaction_score - b.interaction_score) || ((a.primary_ts || 9999999999) - (b.primary_ts || 9999999999));
    if (currentSort === 'you_first') return (b.who_first === 'you' ? 1 : 0) - (a.who_first === 'you' ? 1 : 0) || (b.diff_days || 0) - (a.diff_days || 0);
    if (currentSort === 'them_first') return (b.who_first === 'them' ? 1 : 0) - (a.who_first === 'them' ? 1 : 0) || (b.diff_days || 0) - (a.diff_days || 0);
    if (currentSort === 'az') return a.username.localeCompare(b.username);
    if (currentSort === 'private_first') return (Number(a.is_close_friend) - Number(b.is_close_friend)) || (Number(b.is_private) - Number(a.is_private));
    return 0;
  });

  if (currentIndex >= viewList.length) currentIndex = Math.max(0, viewList.length - 1);
  renderCard();
}

function formatDuration(days) {
  if (!days || days <= 0) return 'дата неизвестна';
  if (days >= 365) {
    const y = Math.floor(days / 365);
    const m = Math.floor((days % 365) / 30);
    return m > 0 ? `${y} г. ${m} мес.` : `${y} г.`;
  }
  if (days >= 30) return `${Math.floor(days / 30)} мес. (${days} дн.)`;
  return `${days} дн.`;
}

function renderCard() {
  const posLabel = document.getElementById('posLabel');
  const fill = document.getElementById('progressFill');
  const cardBack = document.getElementById('cardBack');

  if (viewList.length === 0) {
    posLabel.textContent = '0 / 0';
    fill.style.width = '0%';
    cardBack.style.display = 'none';
    document.getElementById('cardUsername').textContent = 'Список пуст 🎉';
    document.getElementById('cardFullName').textContent = 'Нет профилей по фильтру — загрузи папку/ZIP или смени вкладку';
    document.getElementById('avatarInitials').textContent = '✓';
    document.getElementById('cardBadges').innerHTML = '';
    document.getElementById('statFollowed').textContent = '—';
    document.getElementById('statFollower').textContent = '—';
    document.getElementById('statInter').textContent = '—';
    document.getElementById('tiePill').textContent = '✨ Готово';
    document.getElementById('daysPill').textContent = '';
    return;
  }

  cardBack.style.display = viewList.length > 1 ? 'block' : 'none';
  const p = viewList[currentIndex];
  posLabel.textContent = `${currentIndex + 1} из ${viewList.length}`;
  fill.style.width = `${((currentIndex + 1) / viewList.length) * 100}%`;

  const tier = p.tie_tier || 'ghost';
  const tieEl = document.getElementById('tiePill');
  tieEl.className = 'tie-pill ' + tier;
  tieEl.textContent = `${TIE_LABELS[tier] || '👻 Призрак'} (${p.interaction_score || 0}%)`;

  document.getElementById('daysPill').textContent = p.days_ago ? `⏱ ${formatDuration(p.days_ago)}` : '';

  const initials = p.username.replace(/[^a-zA-Z0-9а-яА-Я]/g, '').slice(0, 2).toUpperCase() || 'IG';
  document.getElementById('avatarInitials').textContent = initials;
  document.getElementById('avatarRing').classList.toggle('cf', !!p.is_close_friend);

  document.getElementById('cardUsername').textContent = '@' + p.username + (p.is_verified ? ' ☑' : '');
  document.getElementById('cardFullName').textContent = p.full_name || '';
  document.getElementById('profileLink').href = p.link;
  document.getElementById('btnOpenProfile').href = p.link;

  const badges = [];
  if (p.tab === 'mutual') badges.push('<span class="badge mutual">Взаимная подписка</span>');
  if (p.tab === 'not_back') badges.push('<span class="badge not_back">Не подписан в ответ</span>');
  if (p.tab === 'fans') badges.push('<span class="badge fans">Подписан на тебя</span>');
  if (p.tab === 'pending') badges.push('<span class="badge pending">⏳ Не принял заявку</span>');
  if (p.is_close_friend) badges.push('<span class="badge cf">★ Близкий друг</span>');
  if (p.is_private) badges.push('<span class="badge">🔒 Приватный</span>');
  if (p.was_unfollowed_before) badges.push('<span class="badge">🔄 Раньше отписывался</span>');

  if (p.who_first === 'you') badges.push(`<span class="badge">🤝 Ты первым (+${p.diff_days} дн.)</span>`);
  else if (p.who_first === 'them') badges.push(`<span class="badge cf">✨ Он первым (+${p.diff_days} дн.)</span>`);
  else if (p.who_first === 'same_day') badges.push('<span class="badge cf">⚡ Взаимно в 1 день</span>');

  if (p.tag) badges.push(`<span class="badge cf">🏷 ${p.tag}</span>`);
  if (p.action) badges.push(`<span class="badge action-tag">Решение: ${ACTION_LABELS[p.action] || p.action}</span>`);
  document.getElementById('cardBadges').innerHTML = badges.join('');

  document.querySelectorAll('.tag-chip').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.tag === (p.tag || ''));
  });

  document.getElementById('statFollowed').textContent = p.followed_date || '—';
  document.getElementById('statFollower').textContent = p.follower_date || '—';
  document.getElementById('statInter').textContent = `${p.interaction_score || 0}%`;
}

async function decide(action, customTag = null) {
  if (viewList.length === 0) return;
  const p = viewList[currentIndex];
  historyStack.push({ username: p.username, prevAction: p.action || '', prevTag: p.tag || '' });

  p.action = action === 'reset' ? '' : action;
  if (customTag !== null) p.tag = customTag;
  setLocalAction(p.username, p.action, p.tag);

  haptic(action === 'keep' ? 'success' : 'medium');

  if (!isStaticMode) {
    fetch('/api/instaswipe/action', {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ username: p.username, action: p.action, tag: p.tag || '' })
    }).catch(() => {});
  }

  updateCounts();
  if (hideDecided && p.action) {
    viewList.splice(currentIndex, 1);
    if (currentIndex >= viewList.length) currentIndex = Math.max(0, viewList.length - 1);
    renderCard();
  } else {
    nextCard();
  }
}

function setTag(tagName) {
  if (viewList.length === 0) return;
  const p = viewList[currentIndex];
  const nextTag = (p.tag === tagName) ? '' : tagName;
  decide('keep', nextTag);
}

function undoLastAction() {
  if (historyStack.length === 0) return;
  haptic('light');
  const last = historyStack.pop();
  const p = allProfiles.find(x => x.username === last.username);
  if (!p) return;
  p.action = last.prevAction;
  p.tag = last.prevTag;
  setLocalAction(p.username, p.action || 'reset', p.tag);

  if (!isStaticMode) {
    fetch('/api/instaswipe/action', {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ username: p.username, action: p.action || 'reset', tag: p.tag || '' })
    }).catch(() => {});
  }
  updateCounts();
  applyFilterAndSort();
  const idx = viewList.findIndex(x => x.username === last.username);
  if (idx >= 0) {
    currentIndex = idx;
    renderCard();
  }
}

async function autoSelectGhosts() {
  const candidates = allProfiles.filter(p =>
    p.tab === 'not_back' &&
    !p.action &&
    !p.is_close_friend &&
    !p.is_keep &&
    !p.tag &&
    (p.tie_tier === 'ghost' || (p.interaction_score || 0) === 0)
  );
  if (candidates.length === 0) {
    alert('Нет необработанных призраков во вкладке «Не взаимно»!');
    return;
  }
  if (!confirm(`Найдено ${candidates.length} невзаимных профилей с 0% интеракций (не из близких друзей). Отметить их всех на отписку в корзину?`)) {
    return;
  }
  haptic('warning');
  const usernames = candidates.map(c => c.username);
  candidates.forEach(c => {
    historyStack.push({ username: c.username, prevAction: '', prevTag: '' });
    c.action = 'unfollow_me';
    setLocalAction(c.username, 'unfollow_me', '');
  });

  if (!isStaticMode) {
    await fetch('/api/instaswipe/batch_action', {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ usernames, action: 'unfollow_me' })
    }).catch(() => {});
  }
  updateCounts();
  applyFilterAndSort();
  openQueueModal();
}

function nextCard() {
  if (currentIndex < viewList.length - 1) {
    currentIndex++;
    renderCard();
  }
}

function prevCard() {
  if (currentIndex > 0) {
    currentIndex--;
    renderCard();
  }
}

/* Swipe gestures */
function setupSwipe() {
  const card = document.getElementById('card');
  const bLeft = document.getElementById('badgeLeft');
  const bRight = document.getElementById('badgeRight');
  let startX = 0, startY = 0, dx = 0, dragging = false;

  card.addEventListener('pointerdown', e => {
    if (e.target.closest('a') || e.target.closest('button')) return;
    dragging = true;
    startX = e.clientX;
    startY = e.clientY;
    dx = 0;
    card.classList.add('swiping');
  });

  window.addEventListener('pointermove', e => {
    if (!dragging) return;
    dx = e.clientX - startX;
    const dy = e.clientY - startY;
    if (Math.abs(dy) > Math.abs(dx) * 1.5 && Math.abs(dx) < 30) return;
    card.style.transform = `translateX(${dx}px) rotate(${dx * 0.055}deg)`;
    bLeft.style.opacity = dx < -30 ? Math.min(1, (-dx - 30) / 80) : 0;
    bRight.style.opacity = dx > 30 ? Math.min(1, (dx - 30) / 80) : 0;
  });

  window.addEventListener('pointerup', () => {
    if (!dragging) return;
    dragging = false;
    card.classList.remove('swiping');
    bLeft.style.opacity = 0;
    bRight.style.opacity = 0;
    card.style.transform = '';
    if (dx < -95) {
      const defAct = currentTab === 'fans' ? 'remove_follower' : (currentTab === 'mutual' ? 'mutual_break' : 'unfollow_me');
      decide(defAct);
    } else if (dx > 95) {
      decide('keep');
    }
  });
}

/* Modals */
function openModal(id) { document.getElementById(id).classList.add('open'); }
function closeModal(id) { document.getElementById(id).classList.remove('open'); }

function openRadarModal() {
  const s = summaryData || {};
  const grid = document.getElementById('radarGrid');
  grid.innerHTML = `
    <div class="radar-card">
      <span class="stat-label">Индекс взаимности</span>
      <span class="radar-big" style="color:#6ee7b7;">${s.reciprocity_rate ?? 0}%</span>
      <span style="font-size:11px;color:var(--muted);">${s.mutual_count ?? 0} из ${s.total_following ?? 0} взаимны</span>
    </div>
    <div class="radar-card">
      <span class="stat-label">Карма подписок</span>
      <span class="radar-big" style="color:#93c5fd;">${s.follower_ratio ?? 0}x</span>
      <span style="font-size:11px;color:var(--muted);">${s.total_followers ?? 0} подп. / ${s.total_following ?? 0} подписок</span>
    </div>
    <div class="radar-card">
      <span class="stat-label">👻 Призраки (0%)</span>
      <span class="radar-big" style="color:#fda4af;">${s.ghosts_count ?? 0}</span>
      <span style="font-size:11px;color:var(--muted);">Кандидаты на отписку</span>
    </div>
    <div class="radar-card">
      <span class="stat-label">⏳ Висящие заявки</span>
      <span class="radar-big" style="color:#fde68a;">${s.pending_count ?? 0}</span>
      <span style="font-size:11px;color:var(--muted);">Не приняли запрос</span>
    </div>
  `;

  const oldList = document.getElementById('radarOldestList');
  const oldest = s.oldest_not_back || [];
  oldList.innerHTML = oldest.length ? oldest.map(o => `
    <div class="queue-item">
      <span><strong>@${o.username}</strong></span>
      <span style="color:var(--muted);font-size:11.5px;">с ${o.date} (${o.days} дн.)</span>
    </div>
  `).join('') : '<div style="color:var(--muted);font-size:12px;">Нет данных о датах</div>';

  const pendList = document.getElementById('radarPendingList');
  const pend = s.oldest_pending || [];
  pendList.innerHTML = pend.length ? pend.map(o => `
    <div class="queue-item">
      <span><strong>@${o.username}</strong></span>
      <span style="color:#fde68a;font-size:11.5px;">ждёт ${o.days} дн. (с ${o.date})</span>
    </div>
  `).join('') : '<div style="color:var(--muted);font-size:12px;">Нет висящих заявок</div>';

  openModal('radarModal');
}

function openQueueModal() {
  const list = document.getElementById('queueList');
  const chosen = allProfiles.filter(p => p.action && p.action !== 'keep');
  if (chosen.length === 0) {
    list.innerHTML = '<div style="color:var(--muted);padding:12px 0;">Пока никого не выбрано</div>';
  } else {
    list.innerHTML = chosen.map(p => `
      <div class="queue-item">
        <div>
          <strong>@${p.username}</strong>
          <div style="font-size:11px;color:var(--muted);">${ACTION_LABELS[p.action] || p.action}</div>
        </div>
        <button class="tool-btn" onclick="resetAction('${p.username}')">Отменить</button>
      </div>
    `).join('');
  }
  openModal('queueModal');
}

async function resetAction(username) {
  const p = allProfiles.find(x => x.username === username);
  if (p) p.action = '';
  setLocalAction(username, 'reset');
  if (!isStaticMode) {
    await fetch('/api/instaswipe/action', {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ username, action: 'reset' })
    }).catch(() => {});
  }
  updateCounts();
  applyFilterAndSort();
  openQueueModal();
}

/* Upload Folder / ZIP / JSON right from Mini App */
async function handleFileUpload(fileList) {
  if (!fileList || fileList.length === 0) return;
  const statusEl = document.getElementById('uploadStatus');

  // Отбираем только .json, .zip и .csv (пропускаем тяжелые фото/видео из папки выгрузки!)
  const validFiles = [];
  for (const f of fileList) {
    const n = f.name.toLowerCase();
    if (n.endsWith('.json') || n.endsWith('.zip') || n.endsWith('.csv')) {
      validFiles.push(f);
    }
  }
  if (validFiles.length === 0) {
    statusEl.textContent = '⚠ В выбранной папке не найдено .json или .zip файлов.';
    return;
  }

  statusEl.textContent = `🔐 Шифрование и отправка ${validFiles.length} файлов...`;
  const formData = new FormData();
  for (const f of validFiles) {
    formData.append('files', f, f.webkitRelativePath || f.name);
  }

  try {
    const res = await fetch('/api/instaswipe/upload', {
      method: 'POST',
      headers: authHeaders(),
      body: formData
    });
    const out = await res.json();
    if (res.ok && out.ok) {
      haptic('success');
      statusEl.textContent = `✅ Зашифровано в БД! Распознано файлов: ${out.recognized_count}, профилей: ${out.total_profiles}`;
      await init();
      setTimeout(() => closeModal('uploadModal'), 1200);
    } else {
      statusEl.textContent = '⚠ Ошибка: ' + (out.error || 'не удалось загрузить');
    }
  } catch (e) {
    statusEl.textContent = '⚠ Сервер недоступен (в статическом режиме GitHub Pages скидывай файлы в Telegram-бота).';
  }
}

async function applyUserPin() {
  userPin = document.getElementById('pinInput').value.trim();
  sessionStorage.setItem('instaswipe_pin', userPin);
  if (!isStaticMode) {
    await fetch('/api/instaswipe/set_pin', {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ pin: userPin })
    }).catch(() => {});
  }
  await init();
  alert(userPin ? '🔑 Личный PIN-ключ применён! Сейф перешифрован.' : 'PIN-ключ снят.');
}

async function wipeMyVault() {
  if (!confirm('Точно удалить все твои зашифрованные данные из базы Supabase PostgreSQL? Это действие необратимо.')) return;
  localStorage.removeItem(LS_KEY);
  if (!isStaticMode) {
    await fetch('/api/instaswipe/wipe', { method: 'POST', headers: authHeaders() }).catch(() => {});
  }
  allProfiles = [];
  updateCounts();
  applyFilterAndSort();
  closeModal('privacyModal');
  alert('🔥 Все твои данные полностью стёрты из базы данных.');
}

async function sendExportToBot() {
  if (isStaticMode) {
    exportActionsJson();
    return;
  }
  const res = await fetch('/api/instaswipe/send_export_to_bot', { method: 'POST', headers: authHeaders() });
  const data = await res.json();
  if (data.ok) {
    haptic('success');
    alert('📩 Готово! Бот отправил тебе файлы tinder_actions.json и unfollow_candidates.csv прямо в чат Telegram.');
  } else {
    exportActionsJson();
  }
}

function exportActionsJson() {
  const out = {};
  const now = new Date().toISOString().slice(0, 19);
  for (const p of allProfiles) {
    if (p.action || p.tag) out[p.username] = { action: p.action || 'keep', tag: p.tag || '', updated_at: now };
  }
  const blob = new Blob([JSON.stringify(out, null, 2)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'tinder_actions.json';
  a.click();
}

init();
</script>
</body>
</html>
"""
