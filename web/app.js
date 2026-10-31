(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const token = document.querySelector('meta[name="pocket-token"]').content;
  const nativeAndroid = navigator.userAgent.includes('PocketStashAndroid/');
  document.documentElement.dataset.native = String(nativeAndroid);
  // Dark by default. A normal browser can switch themes here; the Android
  // shell uses its own theme control and syncs the preference into this page.
  function applyAppearance(theme) {
    const chosen = theme === 'light' ? 'light' : 'dark';
    document.documentElement.dataset.theme = chosen;
    const button = $('themeButton');
    const label = chosen === 'dark' ? 'Switch to light mode' : 'Switch to dark mode';
    button.setAttribute('aria-label', label);
    button.setAttribute('title', label);
    button.querySelector('use').setAttribute('href', chosen === 'dark' ? '#i-sun' : '#i-moon');
    document.querySelector('meta[name="theme-color"]').content = chosen === 'dark' ? '#101914' : '#f7f8f5';
    try { localStorage.setItem('pocket-theme', chosen); } catch (_) { /* private browsing may disable storage */ }
  }
  let savedTheme = 'dark';
  try { savedTheme = localStorage.getItem('pocket-theme') || 'dark'; } catch (_) { /* use dark */ }
  if (nativeAndroid) {
    const requested = new URLSearchParams(window.location.search).get('appearance');
    if (requested === 'dark' || requested === 'light') savedTheme = requested;
  }
  applyAppearance(savedTheme);
  $('themeButton').addEventListener('click', () => applyAppearance(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'));
  try { $('scanPhashes').checked = localStorage.getItem('pocket-scan-phashes') !== 'off'; } catch (_) { /* default on */ }
  $('scanPhashes').addEventListener('change', () => {
    try { localStorage.setItem('pocket-scan-phashes', $('scanPhashes').checked ? 'on' : 'off'); } catch (_) { /* optional preference */ }
  });
  // The Android shell blocks new windows; a direct HTTPS link can open in the browser.
  if (nativeAndroid) {
    document.querySelectorAll('a[target="_blank"]').forEach(link => link.removeAttribute('target'));
  }
  let status = null;
  let draft = [];
  let fingerprint = null;
  let dirty = false;
  let browser = null;
  let browserPurpose = 'library';
  let wfPlan = null;
  let wfChoices = {};
  let wfExcluded = new Set();
  let wfSnapshot = '';
  let wfRunning = false;
  let wfBusy = false;
  let wfPolling = false;
  let refreshing = false;
  let busy = false;
  let toastTimer;

  async function api(path, body) {
    const options = body === undefined ? {} : {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-Pocket-Token': token},
      body: JSON.stringify(body)
    };
    const response = await fetch(path, options);
    let data;
    try { data = await response.json(); } catch (_) { throw new Error('The companion returned an unexpected response.'); }
    if (!response.ok) {
      const error = new Error(data.error || 'Something went wrong.');
      error.status = response.status;
      throw error;
    }
    return data;
  }
  function toast(message, kind = '') {
    const el = $('toast');
    clearTimeout(toastTimer);
    el.textContent = message;
    el.className = 'toast' + (kind ? ' ' + kind : '');
    el.hidden = false;
    toastTimer = setTimeout(() => { el.hidden = true; }, kind ? 6500 : 4000);
  }
  const icon = (name) => {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.classList.add('icon');
    svg.setAttribute('aria-hidden', 'true');
    const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', '#i-' + name);
    svg.append(use);
    return svg;
  };
  function setButton(button, label, iconName) {
    button.replaceChildren();
    if (iconName) button.append(icon(iconName));
    button.append(document.createTextNode(label));
  }
  function isFolderReady() {
    return !!status && status.server.state === 'running' && ['SETUP', 'OK'].includes(status.server.system) && Array.isArray(status.folders);
  }
  function openDialog(id) {
    document.querySelectorAll('dialog[open]').forEach(dialog => dialog.close());
    $(id).showModal();
  }
  async function refresh() {
    if (refreshing) return;
    refreshing = true;
    try {
      const data = await api('/api/status');
      status = data;
      if (!dirty && Array.isArray(data.folders)) {
        draft = data.folders.map(f => ({...f}));
        fingerprint = data.fingerprint;
      } else if (!dirty && data.folders === null) {
        draft = [];
        fingerprint = null;
      }
      render();
    } catch (error) {
      $('serverSubtitle').textContent = 'Connection to the companion lost.';
      $('serverTitle').textContent = 'Unable to connect';
    } finally { refreshing = false; }
  }
  function setStep(id, label, done, warn = false) {
    const el = $(id);
    el.textContent = label;
    el.classList.toggle('good', done);
    el.classList.toggle('warn', warn && !done);
  }
  function render() {
    if (!status) return;
    $('appVersion').textContent = 'v' + status.version;
    const toolsReady = status.tools.stash && status.tools.ffmpeg && status.tools.ffprobe;
    const installed = toolsReady;
    const storage = status.storage.available;
    const configured = status.server.system === 'OK' && Array.isArray(status.folders) && status.folders.length > 0;
    const ready = isFolderReady();
    const installationRunning = status.install.state === 'running';
    $('demoBanner').hidden = !status.demo;
    setStep('installState', installed ? 'READY' : installationRunning ? 'INSTALLING' : status.install.state === 'failed' ? 'NEEDS ATTENTION' : 'NOT INSTALLED', installed, status.install.state === 'failed');
    setStep('storageState', storage ? 'CONNECTED' : 'PERMISSION NEEDED', storage, !storage);
    setStep('foldersState', configured ? 'ADDED' : 'NEXT UP', configured);
    $('numInstall').classList.toggle('done', installed);
    $('numStorage').classList.toggle('done', storage);
    $('numFolders').classList.toggle('done', configured);
    $('numInstall').textContent = installed ? '✓' : '01';
    $('numStorage').textContent = storage ? '✓' : '02';
    $('numFolders').textContent = configured ? '✓' : '03';
    const count = [installed, storage, configured].filter(Boolean).length;
    $('progressText').textContent = `${count} of 3 complete`;
    $('progressFill').style.width = `${count / 3 * 100}%`;
    $('installDescription').textContent = installed ? 'Stash and FFmpeg are available on this device.' : 'Get Stash and FFmpeg from Termux repositories.';
    $('installButton').disabled = installed || installationRunning || busy;
    $('installButton').textContent = installed ? 'Installed' : installationRunning ? 'Installing…' : status.install.state === 'failed' ? 'Retry' : 'Install';
    $('storageDescription').textContent = storage ? 'Termux can read shared phone storage.' : 'Allow Termux to see your phone’s shared folders.';
    $('storageButton').textContent = storage ? 'Details' : 'How to';
    $('chooseButton').disabled = !ready || busy;
    $('chooseButton').textContent = configured ? 'Manage' : 'Choose';
    $('installProgress').hidden = !installationRunning && status.install.state !== 'failed';
    $('installProgressText').textContent = status.install.step || 'Installing…';
    $('installProgress').querySelector('.spinner').hidden = !installationRunning;
    $('installProgress').querySelector('.indeterminate').hidden = !installationRunning;
    renderServer();
    renderTools();
    renderFolders();
    document.querySelectorAll('.wf-native').forEach(link => { link.hidden = !nativeAndroid || status.demo; });
  }
  function renderServer() {
    const s = status.server;
    let title = 'Stash is stopped', sub = 'Ready when you are.', live = 'OFFLINE';
    let buttonLabel = 'Start Stash', buttonIcon = 'power';
    let canToggle = !busy && status.install.state !== 'running';
    let canOpen = false, liveClass = '';
    if (!status.tools.stash && s.state === 'stopped') {
      title = 'Not installed yet'; sub = 'Let’s get the essentials first.'; buttonLabel = 'Install Stash';
    } else if (s.state === 'starting') {
      title = 'Starting Stash'; sub = s.message; live = 'STARTING'; liveClass = 'warn';
      buttonLabel = s.managed ? 'Stop starting' : 'Starting…'; canToggle = canToggle && s.managed;
    } else if (s.state === 'running') {
      title = 'Stash is running'; sub = s.managed ? 'Your library is ready when you are.' : 'Running outside this companion.';
      live = 'ONLINE'; liveClass = 'online'; buttonLabel = s.managed ? 'Stop Stash' : 'Running separately'; canToggle = canToggle && s.managed; canOpen = true;
      if (s.system === 'SETUP') sub = 'Choose a folder to finish setup.';
      if (s.system === 'AUTH_REQUIRED') sub = 'API key required to manage folders.';
      if (s.system === 'SECURITY_BLOCKED') sub = 'Stash security check blocked access; see logs.';
      if (s.system === 'NEEDS_MIGRATION') sub = 'Open Stash to complete migration.';
    } else if (s.state === 'conflict') {
      title = 'Port in use'; sub = s.message; live = 'CHECK'; liveClass = 'warn'; buttonLabel = 'Port unavailable'; canToggle = false;
    } else if (s.state === 'error') {
      title = 'Stash needs attention'; sub = 'Open the log for more information.'; live = 'CHECK'; liveClass = 'warn'; buttonLabel = 'Try starting again';
    }
    $('serverTitle').textContent = title;
    $('serverSubtitle').textContent = sub;
    $('serverLive').className = 'live-label' + (liveClass ? ' ' + liveClass : '');
    $('serverLive').replaceChildren(document.createElement('i'), document.createTextNode(' ' + live));
    $('serverSymbol').classList.toggle('running', s.state === 'running');
    $('serverAddress').textContent = `127.0.0.1:${s.port}`;
    setButton($('serverButton'), buttonLabel, buttonIcon);
    $('serverButton').disabled = !canToggle;
    $('serverOpenButton').disabled = !canOpen;
    $('openStashHeader').disabled = !canOpen;
    $('serverFootnote').textContent = s.state === 'running' && !s.managed ? 'This process was not started here; its network settings may differ.' : 'Keep Termux open while you’re using Stash.';
    let heroLabel = 'Get started', heroIcon = 'arrow-right';
    if (status.install.state === 'running') heroLabel = 'Installing…';
    else if (s.system === 'AUTH_REQUIRED') heroLabel = 'Connect Stash';
    else if (s.system === 'NEEDS_MIGRATION' || s.system === 'SECURITY_BLOCKED') heroLabel = 'Open Stash';
    else if (isFolderReady() && !(status.folders || []).length) heroLabel = 'Choose a folder';
    else if (isFolderReady()) heroLabel = 'Open your library';
    else if (status.tools.stash && s.state === 'stopped') heroLabel = 'Start Stash';
    else if (s.state === 'starting') heroLabel = 'Starting…';
    $('heroAction').replaceChildren(document.createTextNode(heroLabel), icon(heroIcon));
    $('heroAction').disabled = busy || status.install.state === 'running' || s.state === 'starting' || s.state === 'conflict';
  }
  function renderTools() {
    for (const [element, available] of [[$('stashTool'), status.tools.stash], [$('ffmpegTool'), status.tools.ffmpeg && status.tools.ffprobe]]) {
      element.classList.toggle('online', available);
      element.replaceChildren(document.createElement('i'), document.createTextNode(available ? ' Ready' : ' Not installed'));
    }
  }
  function renderFolders() {
    const ready = isFolderReady();
    $('folderCount').textContent = String(draft.length);
    $('addFolderButton').disabled = !ready || busy;
    $('scanButton').disabled = !ready || !draft.length || dirty || busy;
    const activeTask = (status.jobs || []).some(job => ['READY', 'RUNNING', 'STOPPING'].includes(job.status));
    $('generatePhashesButton').disabled = !ready || dirty || busy || activeTask || !draft.some(folder => !folder.excludeVideo);
    $('scanPhashes').disabled = busy;
    $('draftActions').hidden = !dirty;
    $('saveOnlyButton').disabled = !ready || busy;
    $('saveScanButton').disabled = !ready || busy || !draft.length;
    const list = $('folderList');
    list.replaceChildren();
    if (!draft.length) {
      const empty = document.createElement('div'); empty.className = 'folder-empty';
      const illustration = document.createElement('div'); illustration.className = 'empty-illustration'; illustration.append(icon('folder'));
      const copy = document.createElement('div'); const title = document.createElement('strong');
      title.textContent = ready ? 'A little room for your library' : 'Your folders will show up here';
      const description = document.createElement('p');
      description.textContent = ready ? 'Add your first folder to tell Stash where to look.' : 'Once Stash is running, choose the folders you want to scan.';
      copy.append(title, description); empty.append(illustration, copy); list.append(empty);
    } else {
      draft.forEach((folder, index) => {
        const row = document.createElement('div'); row.className = 'folder-row';
        const folderIcon = document.createElement('span'); folderIcon.className = 'folder-row-icon'; folderIcon.append(icon('folder'));
        const copy = document.createElement('div'); copy.className = 'folder-row-copy';
        const title = document.createElement('strong'); title.textContent = folder.path.split('/').filter(Boolean).pop() || folder.path;
        const path = document.createElement('code'); path.textContent = folder.path; path.title = folder.path;
        copy.append(title, path);
        const select = document.createElement('select'); select.className = 'folder-type'; select.setAttribute('aria-label', `Media types for ${title.textContent}`);
        for (const [value, label] of [['both', 'Videos + images'], ['videos', 'Videos only'], ['images', 'Images only']]) {
          const option = document.createElement('option'); option.value = value; option.textContent = label; select.append(option);
        }
        if (folder.excludeVideo && folder.excludeImage) {
          const option = document.createElement('option'); option.value = 'none'; option.textContent = 'Excluded'; select.append(option);
        }
        select.value = folder.excludeVideo ? (folder.excludeImage ? 'none' : 'images') : (folder.excludeImage ? 'videos' : 'both');
        select.disabled = !ready || busy;
        select.addEventListener('change', () => {
          folder.excludeVideo = select.value === 'images' || select.value === 'none';
          folder.excludeImage = select.value === 'videos' || select.value === 'none';
          dirty = true; render();
        });
        const remove = document.createElement('button'); remove.className = 'icon-button remove-folder';
        remove.setAttribute('aria-label', `Remove ${title.textContent}`); remove.append(icon('trash')); remove.disabled = busy;
        remove.addEventListener('click', () => { draft.splice(index, 1); dirty = true; render(); });
        row.append(folderIcon, copy, select, remove); list.append(row);
      });
    }
    let hint = 'Start Stash to manage your folders.';
    if (ready && !draft.length) hint = 'Folders are only changed when you save.';
    else if (ready) hint = dirty ? 'Save changes before starting a scan.' : 'Other scan options use Stash’s task settings.';
    if (status.server.system === 'AUTH_REQUIRED') hint = 'Add your Stash API key in Connection settings.';
    if (status.server.system === 'SECURITY_BLOCKED') hint = 'Check the Stash server log for its security warning.';
    if (status.server.system === 'NEEDS_MIGRATION') hint = 'Open Stash to complete its database migration.';
    if (status.folder_error) hint = status.folder_error;
    const activeJob = (status.jobs || []).find(job => ['READY', 'RUNNING', 'STOPPING'].includes(job.status));
    if (activeJob && !dirty) hint = `${activeJob.description || 'Stash job'} ${activeJob.status.toLowerCase()}…`;
    $('folderHint').textContent = hint;
  }
  async function runAction(fn) {
    if (busy) return;
    busy = true; render();
    try { await fn(); }
    catch (error) { toast(error.message || 'Something went wrong.', 'error'); }
    finally { busy = false; await refresh(); render(); }
  }
  async function install() {
    if (status?.install.state === 'running') return;
    await runAction(async () => {
      await api('/api/install', {});
      toast(status.demo ? 'Demo installation started.' : 'Installing Stash and FFmpeg. This may take a little while.');
    });
  }
  async function toggleServer() {
    if (!status) return;
    if (!status.tools.stash && status.server.state === 'stopped') return install();
    await runAction(async () => {
      if (status.server.state === 'running' || status.server.state === 'starting') {
        if (!status.server.managed) throw new Error('This companion can only stop a Stash process it started.');
        const active = (status.jobs || []).some(job => ['READY', 'RUNNING', 'STOPPING'].includes(job.status));
        if (active && !window.confirm('A Stash task is still active. Stop Stash anyway?')) return;
        await api('/api/server/stop', {}); toast('Stash stopped.');
      } else {
        await api('/api/server/start', {}); toast('Starting Stash…');
      }
    });
  }
  function openStash() {
    if (!status || status.server.state !== 'running') return toast('Start Stash first.', 'warning');
    if (status.demo) return toast('This is a demo: there is no real Stash server to open.');
    const url = `http://127.0.0.1:${status.server.port}/`;
    // The Android shell uses a single WebView; ordinary browsers keep a new tab.
    if (navigator.userAgent.includes('PocketStashAndroid/')) window.location.assign(url);
    else window.open(url, '_blank', 'noopener,noreferrer');
  }
  async function heroAction() {
    if (!status) return;
    if (!status.tools.stash) return install();
    if (status.server.system === 'AUTH_REQUIRED') return showConnection();
    if (status.server.system === 'NEEDS_MIGRATION' || status.server.system === 'SECURITY_BLOCKED') return openStash();
    if (isFolderReady() && !status.folders.length) return showBrowser();
    if (isFolderReady()) return openStash();
    if (status.server.state === 'stopped' || status.server.state === 'error') return toggleServer();
  }
  async function loadBrowser(path) {
    const list = $('browserList'); list.replaceChildren();
    const loading = document.createElement('div'); loading.className = 'browser-empty'; loading.textContent = 'Loading folders…'; list.append(loading);
    try {
      browser = await api('/api/browse' + (path ? '?path=' + encodeURIComponent(path) : ''));
      $('currentPath').textContent = browser.current;
      $('currentPath').title = browser.current;
      $('browserBack').disabled = !browser.parent;
      const tabs = $('rootTabs'); tabs.replaceChildren();
      for (const root of browser.roots) {
        const tab = document.createElement('button'); tab.className = 'root-tab';
        tab.textContent = root.label;
        tab.classList.toggle('active', browser.current === root.path || browser.current.startsWith(root.path + '/'));
        tab.addEventListener('click', () => loadBrowser(root.path)); tabs.append(tab);
      }
      list.replaceChildren();
      if (!browser.directories.length) {
        const empty = document.createElement('div'); empty.className = 'browser-empty'; empty.textContent = 'No visible subfolders here.'; list.append(empty);
      } else for (const directory of browser.directories) {
        const button = document.createElement('button'); button.className = 'browser-item';
        const name = document.createElement('span'); name.textContent = directory.name;
        button.append(icon('folder'), name, icon('chevron'));
        button.addEventListener('click', () => loadBrowser(directory.path)); list.append(button);
      }
    } catch (error) {
      list.replaceChildren(); const el = document.createElement('div'); el.className = 'browser-empty'; el.textContent = error.message; list.append(el);
      toast(error.message, 'error');
    }
  }
  async function showBrowser(purpose = 'library') {
    if (!isFolderReady()) return toast('Start Stash and complete any connection steps first.', 'warning');
    browserPurpose = purpose;
    $('folderDialogTitle').textContent = purpose === 'source' ? 'Choose source videos' : purpose === 'destination' ? 'Choose Photos staging folder' : 'Choose a folder';
    $('folderDialogDesc').textContent = purpose === 'library' ? 'Browse folders Termux can read, then add one to your library.' : 'Choose an existing folder. Both source and destination must be inside Stash video libraries.';
    $('folderMediaTypes').hidden = purpose !== 'library';
    $('createFolderButton').hidden = purpose !== 'destination';
    setButton($('selectFolderButton'), purpose === 'library' ? 'Add this folder' : 'Use this folder', purpose === 'library' ? 'plus' : 'check');
    $('includeVideos').checked = true; $('includeImages').checked = true; browser = null;
    openDialog('folderDialog'); await loadBrowser();
  }
  $('browserBack').addEventListener('click', () => { if (browser?.parent) loadBrowser(browser.parent); });
  $('browserRefresh').addEventListener('click', () => loadBrowser(browser?.current));
  $('createFolderButton').addEventListener('click', () => {
    if (!browser || browserPurpose !== 'destination') return;
    $('wfCreateParent').textContent = 'Inside: ' + browser.current;
    $('wfCreateName').value = 'Pocket Stash Photos';
    openDialog('wfCreateDialog');
  });
  $('wfCreateCancel').addEventListener('click', () => openDialog('folderDialog'));
  $('wfCreateConfirm').addEventListener('click', async () => {
    if (!browser) return;
    const button = $('wfCreateConfirm'); button.disabled = true;
    try {
      const result = await api('/api/workflow/create-folder', {parent: browser.current, name: $('wfCreateName').value});
      openDialog('folderDialog'); await loadBrowser(result.path);
      toast(status.demo ? 'Demo folder created. No real files changed.' : 'Empty staging folder created.');
    } catch (error) { toast(error.message, 'error'); }
    finally { button.disabled = false; }
  });
  $('selectFolderButton').addEventListener('click', () => {
    if (!browser) return;
    if (browserPurpose !== 'library') {
      $(browserPurpose === 'source' ? 'wfSource' : 'wfDestination').value = browser.current;
      wfPlan = null; wfChoices = {}; wfExcluded = new Set(); $('wfPlan').hidden = true;
      $('wfRename').checked = false; $('wfConsent').checked = false;
      $('folderDialog').close(); toast('Folder selected. Preview before anything moves.'); return;
    }
    if (!$('includeVideos').checked && !$('includeImages').checked) return toast('Choose videos, images, or both.', 'warning');
    if (draft.some(folder => folder.path === browser.current)) return toast('This folder is already in your list.', 'warning');
    draft.push({path: browser.current, excludeVideo: !$('includeVideos').checked, excludeImage: !$('includeImages').checked});
    dirty = true; $('folderDialog').close(); render(); toast('Folder added. Save when you’re ready.');
  });
  async function saveFolders(scan) {
    if (!dirty || !isFolderReady()) return;
    if (!draft.length && status.folders?.length && !window.confirm('Remove every configured folder? Stash will stop scanning them. Your files won’t be deleted.')) return;
    await runAction(async () => {
      const result = await api('/api/folders', {folders: draft, fingerprint, scan, generate_phashes: $('scanPhashes').checked});
      dirty = false;
      if (result.scan_error) toast('Folders saved, but the scan could not start: ' + result.scan_error, 'warning');
      else if (result.job_id) toast('Folders saved. A scan has been queued in Stash.');
      else toast('Folders saved.');
    });
  }
  async function scan() {
    if (dirty) return toast('Save your changes before scanning.', 'warning');
    await runAction(async () => { await api('/api/scan', {generate_phashes: $('scanPhashes').checked}); toast('Scan queued in Stash.'); });
  }
  async function generatePhashes() {
    if (dirty) return toast('Save your folder changes first.', 'warning');
    if (!window.confirm('Generate missing perceptual hashes for existing videos in your configured video libraries? This can take a while. Existing hashes are not overwritten.')) return;
    await runAction(async () => {
      await api('/api/phashes/generate', {});
      toast('Perceptual hash generation queued in Stash. Follow its progress in Stash tasks.');
    });
  }
  async function stopCompanion() {
    if (!status || status.demo) return toast('Stopping the companion is unavailable in the demo.', 'warning');
    if (status.install.state === 'running') return toast('Wait for installation to finish first.', 'warning');
    if (!window.confirm('Stop the setup server and any Stash process it started? Active scans will stop and unsaved changes will be lost.')) return;
    try {
      await api('/api/companion/stop', {});
      $('helpDialog').close();
      toast('Stopping the setup server. Return to the app or Termux to start it again.');
    } catch (error) { toast(error.message, 'error'); }
  }
  async function loadLogs() {
    try {
      const data = await api('/api/logs');
      $('installLog').textContent = (data.installation || []).join('\n') || 'No installation activity yet.';
      $('stashLog').textContent = (data.stash || []).join('\n') || 'No server output yet.';
      $('installLog').scrollTop = $('installLog').scrollHeight;
      $('stashLog').scrollTop = $('stashLog').scrollHeight;
    } catch (error) { toast(error.message, 'error'); }
  }
  function showConnection() {
    $('apiKey').value = '';
    $('keyConfigured').textContent = status?.server.key_configured ? 'A key is currently saved. Enter a new key to replace it, or leave blank to remove it.' : 'No key currently saved.';
    openDialog('connectionDialog');
  }
  $('keyForm').addEventListener('submit', event => {
    event.preventDefault();
    runAction(async () => {
      await api('/api/key', {key: $('apiKey').value});
      $('apiKey').value = ''; $('connectionDialog').close(); toast('Connection settings saved.');
    });
  });
  async function copy(text) {
    try {
      if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(text);
      else {
        const input = document.createElement('textarea'); input.value = text; input.setAttribute('readonly', '');
        input.style.position = 'fixed'; input.style.opacity = '0'; document.body.append(input); input.select();
        const copied = document.execCommand('copy'); input.remove(); if (!copied) throw new Error('Copy unavailable');
      }
      toast('Copied to clipboard.');
    } catch (_) { toast('Copy manually: ' + text, 'warning'); }
  }
  $('refreshStorage').addEventListener('click', async () => {
    await refresh();
    if (status?.storage.available) { $('storageDialog').close(); toast('Phone storage is connected.'); }
    else toast('Storage is still unavailable. Check the Android permission and try again.', 'warning');
  });
  $('refreshLogs').addEventListener('click', loadLogs);
  $('demoReset').addEventListener('click', () => runAction(async () => {
    await api('/api/demo/reset', {}); dirty = false; draft = []; fingerprint = null; toast('Demo reset.');
  }));
  // The workflow is a dry run until the user explicitly enables renaming and
  // accepts the reviewed moves. No Google Photos state is inferred here.
  function wfElement(tag, cls, text) {
    const el = document.createElement(tag);
    if (cls) el.className = cls;
    if (text !== undefined) el.textContent = text;
    return el;
  }
  function wfPath(label, path) {
    const line = wfElement('div', 'wf-path-line');
    line.append(wfElement('strong', '', label + ' '), document.createTextNode(path || '—'));
    return line;
  }
  function wfUpdateApply() {
    const ready = wfPlan?.items.filter(item => item.state === 'ready').length || 0;
    $('wfApply').disabled = !ready || !$('wfRename').checked || !$('wfConsent').checked || wfBusy || wfRunning;
  }
  function wfRenderPlan() {
    const list = $('wfPlanRows'); list.replaceChildren();
    if (!wfPlan) { $('wfPlan').hidden = true; return; }
    $('wfPlan').hidden = false;
    const count = wfPlan.items.filter(item => item.state === 'ready').length;
    $('wfPlanTitle').textContent = `${count} ready · ${wfPlan.items.length - count} need attention or excluded`;
    if (!wfPlan.items.length) list.append(wfElement('p', 'wf-empty', 'No indexed source videos were found. Scan the source in Stash, then identify its scenes.'));
    for (const item of wfPlan.items) {
      const row = wfElement('div', 'wf-row');
      const top = wfElement('div', 'wf-row-top');
      const include = document.createElement('input');
      include.type = 'checkbox'; include.checked = !wfExcluded.has(item.file_id);
      include.setAttribute('aria-label', 'Include ' + (item.title || item.source));
      include.disabled = !/^\d+$/.test(item.file_id);
      include.addEventListener('change', () => {
        if (include.checked) wfExcluded.delete(item.file_id); else wfExcluded.add(item.file_id);
        wfPreview();
      });
      top.append(include, wfElement('span', 'wf-row-title', item.title || item.source.split('/').pop()),
                 wfElement('span', 'wf-row-state' + (item.state !== 'ready' ? ' warning' : ''), item.state === 'needs_choice' ? 'choose' : item.state));
      row.append(top, wfPath('From', item.source));
      if (item.target) row.append(wfPath('To', item.target));
      if (item.reason) row.append(wfPath('Note', item.reason));
      if (item.performers.length > 1 && item.state !== 'excluded') {
        const select = document.createElement('select');
        select.setAttribute('aria-label', 'Choose performer for ' + (item.title || item.source));
        const initial = wfElement('option', '', 'Choose one performer…'); initial.value = ''; select.append(initial);
        for (const person of item.performers) {
          const option = wfElement('option', '', person.name + (person.disambiguation ? ' (' + person.disambiguation + ')' : ''));
          option.value = person.id; select.append(option);
        }
        select.value = wfChoices[item.scene_id] || item.selected_performer || '';
        select.addEventListener('change', () => {
          if (select.value) wfChoices[item.scene_id] = select.value; else delete wfChoices[item.scene_id];
          wfPreview();
        });
        row.append(select);
      }
      list.append(row);
    }
    wfUpdateApply();
  }
  async function wfPreview() {
    if (wfBusy || wfRunning) return toast('Wait for the current workflow action to finish.', 'warning');
    if (!$('wfSource').value || !$('wfDestination').value) return toast('Choose both workflow folders first.', 'warning');
    wfBusy = true; wfPlan = null; wfUpdateApply();
    $('wfPreviewButton').disabled = true;
    try {
      wfPlan = await api('/api/workflow/preview', {source: $('wfSource').value, destination: $('wfDestination').value,
                         choices: wfChoices, excluded: Array.from(wfExcluded)});
      wfRenderPlan();
      toast('Preview ready. Nothing has moved.');
    } catch (error) { $('wfPlan').hidden = true; toast(error.message, 'error'); }
    finally { wfBusy = false; $('wfPreviewButton').disabled = false; wfUpdateApply(); }
  }
  async function wfApply() {
    if (!wfPlan || $('wfApply').disabled) return;
    const count = wfPlan.items.filter(i => i.state === 'ready').length;
    if (!window.confirm(`Move and rename ${count} original video${count === 1 ? '' : 's'} using the reviewed paths? This is not a backup. Keep other file operations idle.`)) return;
    wfBusy = true; wfUpdateApply();
    try {
      await api('/api/workflow/apply', {plan_id: wfPlan.id, enable_rename: $('wfRename').checked, acknowledged: $('wfConsent').checked});
      wfPlan = null; wfRenderPlan(); $('wfRename').checked = false; $('wfConsent').checked = false;
      wfSnapshot = ''; toast(status.demo ? 'Demo move started. No real files are changed.' : 'Move started. Keep Stash and Termux running.');
      await wfRefresh();
    } catch (error) { toast(error.message, 'error'); }
    finally { wfBusy = false; wfUpdateApply(); }
  }
  async function wfReview(item, backup, album) {
    try {
      await api('/api/workflow/review', {item_id: item.item_id, backup, album});
      wfSnapshot = ''; await wfRefresh();
      toast('Your review was saved. Photos backup is not independently verified.');
    } catch (error) { wfSnapshot = ''; await wfRefresh(); toast(error.message, 'error'); }
  }
  async function wfResolve(item, resolution) {
    if (!window.confirm('Have you inspected BOTH paths and the Stash file entry? This checks your choice but will not change or delete any files.')) return;
    try {
      await api('/api/workflow/resolve', {item_id: item.item_id, resolution, inspected: true});
      wfSnapshot = ''; await wfRefresh(); toast('Journal updated. No files were changed.');
    } catch (error) { toast(error.message, 'error'); }
  }
  function wfRenderHistory(data) {
    const history = $('wfHistory'); history.replaceChildren();
    if (data.history_error) history.append(wfElement('div', 'wf-warning', data.history_error));
    if (!data.runs.length) {
      history.append(wfElement('p', 'wf-empty', 'Moved videos and your review checklist will appear here.'));
      return;
    }
    for (const run of data.runs.slice().reverse()) {
      const block = wfElement('div', 'wf-run');
      const title = wfElement('div', 'wf-run-heading', `Batch · ${run.state.replace('_', ' ')}`);
      const msg = wfElement('p', 'wf-run-msg', run.message || '');
      block.append(title, msg);
      for (const item of run.items) {
        const row = wfElement('div', 'wf-row');
        const top = wfElement('div', 'wf-row-top');
        top.append(wfElement('span', 'wf-row-title', item.title || item.source.split('/').pop()),
                   wfElement('span', 'wf-row-state' + (item.state !== 'moved' ? ' warning' : ''), item.state.replace('_', ' ')));
        row.append(top, wfPath('From', item.source), wfPath('To', item.target));
        if (item.reason) row.append(wfPath('Note', item.reason));
        if (item.state === 'uncertain') {
          const warning = wfElement('p', 'wf-path-line', 'Inspect both locations and Stash. A failed copy can leave a partial target; do not retry blindly.');
          const actions = wfElement('div', 'wf-actions');
          const moved = wfElement('button', 'button button-white button-small', 'Check: moved');
          moved.addEventListener('click', () => wfResolve(item, 'moved'));
          const notMoved = wfElement('button', 'button button-white button-small', 'Check: not moved');
          notMoved.addEventListener('click', () => wfResolve(item, 'not_moved'));
          actions.append(moved, notMoved); row.append(warning, actions);
        }
        if (item.state === 'moved') {
          row.append(wfPath('Matching album', item.album));
          if (!item.local_present) row.append(wfPath('Local file', 'Not present. This does not prove Google Photos has a backup.'));
          const review = wfElement('div', 'wf-review');
          const backupLabel = wfElement('label'); const backup = document.createElement('input');
          backup.type = 'checkbox'; backup.checked = !!item.user_confirmed_backup;
          backupLabel.append(backup, document.createTextNode('I checked this video says Backed up in the correct Google Photos account.'));
          const albumLabel = wfElement('label'); const album = document.createElement('input');
          album.type = 'checkbox'; album.checked = !!item.user_confirmed_album;
          albumLabel.append(album, document.createTextNode('I added this video to the matching performer album and checked it there.'));
          const saveReview = () => {
            const b = backup.checked, a = album.checked;
            backup.disabled = true; album.disabled = true;
            wfReview(item, b, a).finally(() => { backup.disabled = false; album.disabled = false; });
          };
          backup.addEventListener('change', saveReview);
          album.addEventListener('change', saveReview);
          review.append(backupLabel, albumLabel);
          if (backup.checked && album.checked) review.append(wfElement('div', 'wf-path-line', 'Your review complete. Pocket Stash has not checked Google Photos.'));
          if (nativeAndroid && !status?.demo && item.local_present) {
            const actions = wfElement('div', 'wf-actions');
            const link = wfElement('a', 'button button-white button-small wf-link', backup.checked && album.checked ? 'Open video for device-only removal' : 'Open video in Google Photos');
            link.href = 'pocketstash://photos/open?id=' + encodeURIComponent(item.item_id);
            actions.append(link); review.append(actions);
          }
          row.append(review);
        }
        block.append(row);
      }
      history.append(block);
    }
  }
  async function wfRefresh() {
    if (wfPolling) return;
    wfPolling = true;
    try {
      const data = await api('/api/workflow');
      wfRunning = !!data.running; wfUpdateApply();
      const snapshot = JSON.stringify(data);
      if (snapshot !== wfSnapshot) { wfSnapshot = snapshot; wfRenderHistory(data); }
    } catch (_) { /* Existing status card reports connection failures. */ }
    finally { wfPolling = false; }
  }
  $('wfRename').addEventListener('change', wfUpdateApply);
  $('wfConsent').addEventListener('change', wfUpdateApply);

  document.addEventListener('click', event => {
    const close = event.target.closest('[data-close]');
    if (close) { $(close.dataset.close).close(); return; }
    const trigger = event.target.closest('[data-action]');
    if (!trigger || trigger.disabled) return;
    const action = trigger.dataset.action;
    if (action === 'help') openDialog('helpDialog');
    else if (action === 'connection') showConnection();
    else if (action === 'storage') openDialog('storageDialog');
    else if (action === 'logs') { openDialog('logsDialog'); loadLogs(); }
    else if (action === 'browse') showBrowser();
    else if (action === 'wf-source') showBrowser('source');
    else if (action === 'wf-destination') showBrowser('destination');
    else if (action === 'wf-preview') wfPreview();
    else if (action === 'wf-apply') wfApply();
    else if (action === 'wf-refresh') { wfSnapshot = ''; wfRefresh(); }
    else if (action === 'install') install();
    else if (action === 'toggle-server') toggleServer();
    else if (action === 'open-stash') openStash();
    else if (action === 'hero') heroAction();
    else if (action === 'save') saveFolders(false);
    else if (action === 'save-scan') saveFolders(true);
    else if (action === 'scan') scan();
    else if (action === 'generate-phashes') generatePhashes();
    else if (action === 'stop-companion') stopCompanion();
    else if (action === 'discard') { dirty = false; fingerprint = null; refresh(); toast('Unsaved changes discarded.'); }
    else if (action === 'copy') copy(`http://127.0.0.1:${status?.server.port || 9999}/`);
    else if (action === 'copy-command') copy('termux-setup-storage');
  });
  document.querySelectorAll('dialog').forEach(dialog => {
    dialog.addEventListener('click', event => { if (event.target === dialog) dialog.close(); });
  });
  refresh();
  wfRefresh();
  setInterval(refresh, 2500);
  setInterval(wfRefresh, 2500);
})();
