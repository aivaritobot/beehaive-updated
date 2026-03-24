/**
 * beehAIve UPDATED — UI strings ES / EN
 */
(function (global) {
  const STR = {
    es: {
      brand_name: 'beehAIve',
      brand_updated: 'UPDATED',
      tagline:
        'Tu colmena de trabajo: chat multi-modelo, GitHub, archivos, swarm, navegador y terminal — con miel de productividad.',
      nav_chat: 'Chat',
      nav_swarm: 'Swarm',
      nav_files: 'Archivos',
      nav_github: 'GitHub',
      nav_search: 'Internet',
      nav_browser: 'Browser',
      nav_workspace: 'Workspace',
      nav_settings: 'Ajustes',
      conv_list: 'Conversaciones',
      new_chat: '+ Nuevo chat',
      status_chat: 'Chat',
      status_ollama: 'Ollama',
      lang_es: 'ES',
      lang_en: 'EN',
      quick_task_ph: 'Tarea rápida (Enter → chat)',
      quick_send: 'Enviar',
      engine_label: 'Motor',
      engine_drawer: 'Modelo, APIs y opciones avanzadas',
      work_mode: 'Modo work',
      web_search: 'Web search',
      puter_signin: 'Iniciar sesión en Puter',
      puter_signout: 'Cerrar sesión Puter',
      chat_title: 'Chat',
      chat_sub:
        'Elige motor arriba. La terminal rápida ejecuta en tu Workspace. El asistente no corre procesos en segundo plano: tú ejecutas lo que te indique.',
      ctx_ready: 'Contexto para el próximo mensaje',
      clear_ctx: 'Limpiar contexto',
      ws_path: 'Workspace',
      term_quick: 'Terminal rápida',
      term_placeholder: 'Comando shell (ls, git status, pytest…)',
      term_run: 'Ejecutar',
      chat_placeholder: 'Escribe un mensaje…',
      chat_send: 'Enviar',
      upload_label: 'Subir archivo (uploads/)',
      include_upload: 'Incluir último archivo en contexto',
      include_skills: 'Incluir pack de skills',
      footer_line:
        'beehAIve UPDATED — Puter · Groq · OpenRouter · Ollama · swarm multi-LLM',
      footer_claude: 'Abrir /claude (solo Ollama)',
      footer_keys: 'Enter envía',
      settings_title: 'Ajustes',
      swarm_title: 'Swarm',
      files_title: 'Archivos locales',
      github_title: 'GitHub',
      search_title: 'Internet',
      browser_title: 'Browser (Playwright)',
      workspace_title: 'Workspace',
    },
    en: {
      brand_name: 'beehAIve',
      brand_updated: 'UPDATED',
      tagline:
        'Your productivity hive: multi-model chat, GitHub, files, swarm, browser & shell — sweet workflow.',
      nav_chat: 'Chat',
      nav_swarm: 'Swarm',
      nav_files: 'Files',
      nav_github: 'GitHub',
      nav_search: 'Web',
      nav_browser: 'Browser',
      nav_workspace: 'Workspace',
      nav_settings: 'Settings',
      conv_list: 'Conversations',
      new_chat: '+ New chat',
      status_chat: 'Chat',
      status_ollama: 'Ollama',
      lang_es: 'ES',
      lang_en: 'EN',
      quick_task_ph: 'Quick task (Enter → chat)',
      quick_send: 'Send',
      engine_label: 'Engine',
      engine_drawer: 'Model, APIs & advanced',
      work_mode: 'Work mode',
      web_search: 'Web search',
      puter_signin: 'Sign in to Puter',
      puter_signout: 'Sign out Puter',
      chat_title: 'Chat',
      chat_sub:
        'Pick the engine above. Quick terminal runs in your Workspace. The assistant does not run background jobs: you run what it specifies.',
      ctx_ready: 'Context for next message',
      clear_ctx: 'Clear context',
      ws_path: 'Workspace',
      term_quick: 'Quick terminal',
      term_placeholder: 'Shell command (ls, git status, pytest…)',
      term_run: 'Run',
      chat_placeholder: 'Message…',
      chat_send: 'Send',
      upload_label: 'Upload (uploads/)',
      include_upload: 'Include last upload in context',
      include_skills: 'Include skills pack',
      footer_line:
        'beehAIve UPDATED — Puter · Groq · OpenRouter · Ollama · multi-LLM swarm',
      footer_claude: 'Open /claude (Ollama only)',
      footer_keys: 'Enter to send',
      settings_title: 'Settings',
      swarm_title: 'Swarm',
      files_title: 'Local files',
      github_title: 'GitHub',
      search_title: 'Web',
      browser_title: 'Browser (Playwright)',
      workspace_title: 'Workspace',
    },
  };

  function getLang() {
    try {
      return localStorage.getItem('beehaive_lang') || 'es';
    } catch (_) {
      return 'es';
    }
  }

  function setLang(code) {
    if (code !== 'es' && code !== 'en') code = 'es';
    try {
      localStorage.setItem('beehaive_lang', code);
    } catch (_) {}
    document.documentElement.lang = code === 'en' ? 'en' : 'es';
    apply();
  }

  function t(key) {
    const lang = getLang();
    const pack = STR[lang] || STR.es;
    return pack[key] !== undefined ? pack[key] : STR.es[key] || key;
  }

  function apply() {
    const lang = getLang();
    const D = STR[lang] || STR.es;
    document.documentElement.lang = lang === 'en' ? 'en' : 'es';
    document.querySelectorAll('[data-i18n]').forEach(function (el) {
      var key = el.getAttribute('data-i18n');
      if (!key || !D[key]) return;
      var v = D[key];
      var tag = el.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA') {
        var typ = (el.type || '').toLowerCase();
        if (typ === 'button' || typ === 'submit' || typ === 'reset') el.value = v;
        else el.placeholder = v;
      } else {
        el.textContent = v;
      }
    });
    document.querySelectorAll('[data-i18n-title]').forEach(function (el) {
      var key = el.getAttribute('data-i18n-title');
      if (key && D[key]) el.title = D[key];
    });
    document.querySelectorAll('.lang-switch [data-lang]').forEach(function (b) {
      b.classList.toggle('active', b.getAttribute('data-lang') === lang);
    });
  }

  global.BeehaiveI18n = { STR: STR, getLang: getLang, setLang: setLang, apply: apply, t: t };
})(typeof window !== 'undefined' ? window : global);
