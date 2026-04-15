// Trash Tinder — frontend logic (vanilla JS, no build step).
//
// Multi-household capable: the device can hold memberships in several
// households simultaneously (stored in localStorage under a single key),
// with a top-bar pill to switch between them. All API calls are scoped
// to state.currentHousehold / state.currentUser.

// ---------- state ----------
const state = {
  memberships: {},         // { household_id: user_id }
  lastUsed: null,          // household_id
  currentHousehold: null,  // {id, name, invite_code, expected_voters, ...}
  currentUser: null,       // {id, name, household_id, ...}
  users: [],               // members of currentHousehold
  deck: [],
  ongoing: [],
  history: [],
  stats: { pending: 0, kept: 0, tossed: 0, users: 0 },
  config: { locked: false, expected_voters: 0, user_count: 0 },
  view: 'lobby',
  pendingPhoto: null,
  pendingJoin: null,       // { kind: 'create'|'join', household: {...} }
  selectedItemId: null,
};

// ---------- localStorage helpers ----------
const LS_KEY = 'tt_memberships_v1';

function loadMemberships() {
  try {
    const raw = localStorage.getItem(LS_KEY);
    if (!raw) return { memberships: {}, lastUsed: null };
    const parsed = JSON.parse(raw);
    return {
      memberships: parsed.memberships || {},
      lastUsed: parsed.lastUsed || null,
    };
  } catch {
    return { memberships: {}, lastUsed: null };
  }
}
function saveMemberships() {
  localStorage.setItem(LS_KEY, JSON.stringify({
    memberships: state.memberships,
    lastUsed: state.lastUsed,
  }));
}
function addMembership(hhId, userId) {
  state.memberships[hhId] = userId;
  state.lastUsed = hhId;
  saveMemberships();
}
function removeMembership(hhId) {
  delete state.memberships[hhId];
  if (state.lastUsed === hhId) {
    state.lastUsed = Object.keys(state.memberships)[0] || null;
  }
  saveMemberships();
}

// ---------- API ----------
const api = {
  // households
  async createHousehold(name) {
    const r = await fetch('/api/households', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || 'could not create');
    return j.household;
  },
  async getHouseholdByCode(code) {
    const clean = encodeURIComponent(code.toUpperCase().trim());
    const r = await fetch('/api/households/by-code/' + clean);
    if (r.status === 404) return null;
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || 'not found');
    return j.household;
  },
  async getHousehold(hhId) {
    const r = await fetch('/api/households/' + encodeURIComponent(hhId));
    if (r.status === 404) return null;
    const j = await r.json();
    if (!r.ok) return null;
    return j.household;
  },
  async renameHousehold(hhId, name) {
    const r = await fetch('/api/households/' + encodeURIComponent(hhId), {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || 'rename failed');
    return j.household;
  },
  // users
  async createUser(householdId, name) {
    const r = await fetch('/api/users', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ household_id: householdId, name }),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || 'could not join');
    return j.user;
  },
  async me(userId) {
    const r = await fetch('/api/me?user_id=' + encodeURIComponent(userId));
    const j = await r.json();
    return j.user;
  },
  async listUsers(householdId) {
    const r = await fetch('/api/users?household_id=' + encodeURIComponent(householdId));
    if (!r.ok) return [];
    const j = await r.json();
    return j.users || [];
  },
  async deleteUser(userId) {
    const r = await fetch('/api/users/' + encodeURIComponent(userId), { method: 'DELETE' });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || 'delete failed');
    return j;
  },
  // items
  async deck(userId) {
    const r = await fetch('/api/deck?user_id=' + encodeURIComponent(userId));
    if (!r.ok) return [];
    return (await r.json()).items || [];
  },
  async items(householdId) {
    const r = await fetch('/api/items?household_id=' + encodeURIComponent(householdId));
    if (!r.ok) return [];
    return (await r.json()).items || [];
  },
  async itemDetail(itemId) {
    const r = await fetch('/api/items/' + encodeURIComponent(itemId));
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || 'not found');
    return j.item;
  },
  async createItem(blob, title, note, userId, votingRule) {
    const form = new FormData();
    form.append('user_id', userId);
    form.append('title', title);
    form.append('note', note);
    form.append('voting_rule', votingRule || 'keep_wins');
    form.append('photo', blob, 'photo.jpg');
    const r = await fetch('/api/items', { method: 'POST', body: form });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || 'upload failed');
    return j.item;
  },
  async deleteItem(itemId) {
    const r = await fetch('/api/items/' + encodeURIComponent(itemId), { method: 'DELETE' });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || 'delete failed');
    return j;
  },
  // vote
  async vote(itemId, userId, choice) {
    const r = await fetch('/api/vote', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ item_id: itemId, user_id: userId, choice }),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || 'vote failed');
    return j;
  },
  // config
  async getConfig(householdId) {
    const r = await fetch('/api/config?household_id=' + encodeURIComponent(householdId));
    if (!r.ok) return { locked: false, expected_voters: 0, user_count: 0 };
    return await r.json();
  },
  async setLock(householdId, lock) {
    const r = await fetch('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ household_id: householdId, lock }),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || 'lock failed');
    return j;
  },
  async stats(householdId) {
    const r = await fetch('/api/stats?household_id=' + encodeURIComponent(householdId));
    if (!r.ok) return { pending: 0, kept: 0, tossed: 0, users: 0 };
    return await r.json();
  },
  async clearDoneItems(householdId) {
    const r = await fetch('/api/items/clear-done', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ household_id: householdId }),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || 'clear failed');
    return j;
  },
};

// ---------- toast ----------
let toastTimer = null;
function toast(msg) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 2200);
}

function escapeHtml(s) {
  return String(s || '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

// ---------- views / routing ----------
function showView(name) {
  state.view = name;
  document.querySelectorAll('.view').forEach(v => {
    v.hidden = (v.id !== 'view-' + name);
  });
  document.querySelectorAll('.tab').forEach(t => {
    t.classList.toggle('active', t.dataset.view === name);
  });
  const isInApp = !['lobby', 'name'].includes(name);
  document.getElementById('tab-bar').hidden = !isInApp;
  document.getElementById('app-header').hidden = !isInApp;
  document.body.classList.toggle('has-header', isInApp);

  if (name === 'deck') { refreshConfig(); refreshDeck(); }
  else if (name === 'ongoing') refreshOngoing();
  else if (name === 'history') refreshHistory();
  else if (name === 'who') { refreshConfig(); refreshWho(); }

  if (isInApp) startPolling(); else stopPolling();
}

document.getElementById('tab-bar').addEventListener('click', e => {
  const btn = e.target.closest('.tab');
  if (btn) showView(btn.dataset.view);
});

function applyHeaderContext() {
  const nameEl = document.getElementById('household-name');
  const meEl = document.getElementById('me-name');
  if (state.currentHousehold) nameEl.textContent = state.currentHousehold.name;
  if (state.currentUser) meEl.textContent = state.currentUser.name;
}

async function switchToHousehold(hhId) {
  const userId = state.memberships[hhId];
  if (!userId) return false;
  const user = await api.me(userId);
  if (!user) {
    removeMembership(hhId);
    return false;
  }
  const hh = await api.getHousehold(hhId);
  if (!hh) {
    removeMembership(hhId);
    return false;
  }
  state.currentHousehold = hh;
  state.currentUser = user;
  state.lastUsed = hhId;
  saveMemberships();
  state.users = await api.listUsers(hh.id);
  applyHeaderContext();
  showView('deck');
  return true;
}

// ---------- lobby ----------
async function attemptJoinByCode(code, errEl, sourceInputEl) {
  errEl.hidden = true;
  const clean = (code || '').trim().toUpperCase();
  if (!clean) { errEl.textContent = 'Enter a code'; errEl.hidden = false; return; }
  try {
    const hh = await api.getHouseholdByCode(clean);
    if (!hh) { errEl.textContent = 'No household with that code'; errEl.hidden = false; return; }
    state.pendingJoin = { kind: 'join', household: hh };
    if (sourceInputEl) sourceInputEl.value = '';
    renderNameView();
    showView('name');
  } catch (e) {
    errEl.textContent = e.message;
    errEl.hidden = false;
  }
}

async function attemptCreateHousehold(name, errEl, sourceInputEl) {
  errEl.hidden = true;
  const clean = (name || '').trim();
  if (!clean) { errEl.textContent = 'Enter a name'; errEl.hidden = false; return; }
  try {
    const hh = await api.createHousehold(clean);
    state.pendingJoin = { kind: 'create', household: hh };
    if (sourceInputEl) sourceInputEl.value = '';
    renderNameView();
    showView('name');
  } catch (e) {
    errEl.textContent = e.message;
    errEl.hidden = false;
  }
}

function renderNameView() {
  const join = state.pendingJoin;
  if (!join) return;
  document.getElementById('name-brand').textContent =
    join.kind === 'create' ? 'Created' : 'Joining';
  document.getElementById('name-subtitle').textContent = join.household.name;
  const hint = document.getElementById('name-hint');
  hint.textContent = join.kind === 'create'
    ? `Invite code: ${join.household.invite_code} — share it with your family`
    : '';
  document.getElementById('display-name-input').value = '';
  document.getElementById('name-error').hidden = true;
}

async function completeJoin() {
  const name = document.getElementById('display-name-input').value.trim();
  const err = document.getElementById('name-error');
  err.hidden = true;
  if (!name) { err.textContent = 'Enter your name'; err.hidden = false; return; }
  const join = state.pendingJoin;
  if (!join) { showView('lobby'); return; }
  try {
    const user = await api.createUser(join.household.id, name);
    addMembership(join.household.id, user.id);
    state.currentHousehold = join.household;
    state.currentUser = user;
    state.users = await api.listUsers(join.household.id);
    state.pendingJoin = null;
    applyHeaderContext();
    showView('deck');
    if (join.kind === 'create') {
      toast(`Household ready. Code: ${join.household.invite_code}`);
    }
  } catch (e) {
    err.textContent = e.message;
    err.hidden = false;
  }
}

document.getElementById('btn-join-code').addEventListener('click', () => {
  attemptJoinByCode(
    document.getElementById('join-code-input').value,
    document.getElementById('join-error'),
    document.getElementById('join-code-input'),
  );
});
document.getElementById('join-code-input').addEventListener('keydown', e => {
  if (e.key === 'Enter') document.getElementById('btn-join-code').click();
});

document.getElementById('btn-create-household').addEventListener('click', () => {
  attemptCreateHousehold(
    document.getElementById('create-name-input').value,
    document.getElementById('create-error'),
    document.getElementById('create-name-input'),
  );
});
document.getElementById('create-name-input').addEventListener('keydown', e => {
  if (e.key === 'Enter') document.getElementById('btn-create-household').click();
});

document.getElementById('btn-pick-name').addEventListener('click', completeJoin);
document.getElementById('display-name-input').addEventListener('keydown', e => {
  if (e.key === 'Enter') completeJoin();
});

document.getElementById('btn-cancel-name').addEventListener('click', () => {
  state.pendingJoin = null;
  if (Object.keys(state.memberships).length > 0 && state.currentHousehold) {
    showView('deck');
  } else {
    showView('lobby');
  }
});

// ---------- household switcher ----------
const switcherModal = document.getElementById('switcher-modal');

async function openSwitcher() {
  await renderMemberships();
  switcherModal.hidden = false;
}
function closeSwitcher() {
  switcherModal.hidden = true;
}

async function renderMemberships() {
  const list = document.getElementById('memberships-list');
  list.innerHTML = '<div class="muted" style="font-size:13px">Loading...</div>';
  const hhIds = Object.keys(state.memberships);
  if (hhIds.length === 0) {
    list.innerHTML = '<div class="muted" style="font-size:13px">No households yet.</div>';
    return;
  }
  const results = await Promise.all(hhIds.map(id => api.getHousehold(id).catch(() => null)));
  list.innerHTML = '';
  results.forEach((hh, i) => {
    const hhId = hhIds[i];
    if (!hh) {
      removeMembership(hhId);
      return;
    }
    const isCurrent = state.currentHousehold && hh.id === state.currentHousehold.id;
    const row = document.createElement('div');
    row.className = 'membership-row' + (isCurrent ? ' current' : '');
    row.innerHTML = `
      <div>
        <div class="m-name">${escapeHtml(hh.name)}</div>
        <div class="m-sub">${escapeHtml(hh.invite_code)}</div>
      </div>
      <div class="m-chip">${isCurrent ? 'current' : 'switch'}</div>
    `;
    row.addEventListener('click', async () => {
      if (isCurrent) { closeSwitcher(); return; }
      closeSwitcher();
      await switchToHousehold(hh.id);
    });
    list.appendChild(row);
  });
}

document.getElementById('household-pill').addEventListener('click', openSwitcher);
document.getElementById('switcher-close').addEventListener('click', closeSwitcher);
document.getElementById('switcher-backdrop').addEventListener('click', closeSwitcher);

document.getElementById('switcher-join-btn').addEventListener('click', () => {
  const code = document.getElementById('switcher-join-code').value;
  closeSwitcher();
  attemptJoinByCode(
    code,
    document.getElementById('switcher-join-error'),
    document.getElementById('switcher-join-code'),
  );
});
document.getElementById('switcher-join-code').addEventListener('keydown', e => {
  if (e.key === 'Enter') document.getElementById('switcher-join-btn').click();
});

document.getElementById('switcher-create-btn').addEventListener('click', () => {
  const name = document.getElementById('switcher-create-name').value;
  closeSwitcher();
  attemptCreateHousehold(
    name,
    document.getElementById('switcher-create-error'),
    document.getElementById('switcher-create-name'),
  );
});
document.getElementById('switcher-create-name').addEventListener('keydown', e => {
  if (e.key === 'Enter') document.getElementById('switcher-create-btn').click();
});

// ---------- config / lock ----------
async function refreshConfig() {
  if (!state.currentHousehold) return;
  try {
    state.config = await api.getConfig(state.currentHousehold.id);
  } catch {}
  applyConfigUi();
}

function applyConfigUi() {
  const locked = !!state.config.locked;
  const banner = document.getElementById('banner-unlocked');
  if (banner) banner.hidden = locked;
  const box = document.getElementById('lock-box');
  const title = document.getElementById('lock-title');
  const desc = document.getElementById('lock-desc');
  const btn = document.getElementById('btn-lock');
  if (!box) return;
  box.classList.toggle('locked', locked);
  if (locked) {
    title.textContent = `Voting is open (${state.config.expected_voters} people)`;
    desc.textContent = 'Items close as soon as everyone has voted.';
    btn.textContent = 'Unlock (add another person)';
  } else {
    title.textContent = 'Setup mode';
    const n = state.config.user_count || 0;
    desc.textContent = n <= 1
      ? 'Add at least one more person, then tap below to start deciding.'
      : `${n} people joined. Tap below when everyone is here.`;
    btn.textContent = 'Start voting';
  }
}

async function toggleLock() {
  if (!state.currentHousehold) return;
  try {
    const next = !state.config.locked;
    const res = await api.setLock(state.currentHousehold.id, next);
    state.config = res.config;
    applyConfigUi();
    toast(next ? `Voting started — ${state.config.expected_voters} people` : 'Unlocked');
    if (state.view === 'deck') refreshDeck();
    if (state.view === 'who') refreshWho();
  } catch (e) {
    toast(e.message);
  }
}
document.getElementById('btn-lock').addEventListener('click', toggleLock);
document.getElementById('banner-lock-btn').addEventListener('click', toggleLock);

// ---------- deck / swipe ----------
const cardStack = document.getElementById('card-stack');
const emptyDeck = document.getElementById('empty-deck');
let voteInFlight = false;

async function refreshDeck() {
  if (!state.currentUser) return;
  try {
    state.deck = await api.deck(state.currentUser.id);
    if (state.currentHousehold) {
      state.users = await api.listUsers(state.currentHousehold.id);
    }
  } catch { state.deck = []; }
  document.getElementById('deck-count').textContent = state.deck.length;
  renderDeck();
}

function renderDeck() {
  cardStack.innerHTML = '';
  if (state.deck.length === 0) {
    emptyDeck.hidden = false;
    setActionsEnabled(false);
    return;
  }
  emptyDeck.hidden = true;
  const visible = state.deck.slice(0, 3);
  visible.forEach((item, i) => {
    const card = buildCard(item);
    const depth = i;
    card.style.transform = `translateY(${depth * 8}px) scale(${1 - depth * 0.04})`;
    card.style.zIndex = String(10 - depth);
    if (i === 0) attachSwipe(card, item);
    cardStack.appendChild(card);
  });
  setActionsEnabled(true);
}

function setActionsEnabled(on) {
  document.querySelectorAll('.act').forEach(b => { b.disabled = !on; });
}

function buildCard(item) {
  const card = document.createElement('div');
  card.className = 'card';
  card.dataset.itemId = item.id;
  const added = state.users.find(u => u.id === item.created_by);
  const addedBy = added ? added.name : 'someone';
  const total = state.users.length || 1;
  const voted = item.vote_count || 0;
  const title = escapeHtml(item.title || 'Untitled item');
  const note = escapeHtml(item.note || '');
  const rulePill = item.voting_rule === 'majority'
    ? '<span class="rule-pill majority">majority</span>'
    : '<span class="rule-pill">keep wins</span>';
  const skippedTag = item.my_vote === 'skip'
    ? '<div class="round-tag">you skipped</div>' : '';
  card.innerHTML = `
    <div class="img-wrap">
      <img src="/photos/${encodeURIComponent(item.photo_path)}" alt="${title}">
      <div class="badge keep">Keep</div>
      <div class="badge toss">Toss</div>
      <div class="badge skip">Not sure</div>
    </div>
    <div class="meta">
      <div class="title">${title}</div>
      ${note ? `<div class="note">${note}</div>` : ''}
      <div class="subline">
        <div>by ${escapeHtml(addedBy)} · ${voted}/${total} voted · ${rulePill}</div>
        ${skippedTag}
      </div>
    </div>
  `;
  return card;
}

function attachSwipe(card, item) {
  let startX = 0, startY = 0, dx = 0, dy = 0;
  let dragging = false;
  let pointerId = null;
  const keepBadge = card.querySelector('.badge.keep');
  const tossBadge = card.querySelector('.badge.toss');
  const skipBadge = card.querySelector('.badge.skip');

  function onDown(e) {
    if (voteInFlight) return;
    pointerId = e.pointerId;
    card.setPointerCapture(pointerId);
    startX = e.clientX;
    startY = e.clientY;
    dragging = true;
    card.style.transition = 'none';
  }
  function onMove(e) {
    if (!dragging || e.pointerId !== pointerId) return;
    dx = e.clientX - startX;
    dy = e.clientY - startY;
    const rot = dx / 18;
    card.style.transform = `translate(${dx}px, ${dy}px) rotate(${rot}deg)`;
    const horizontal = Math.abs(dx) > Math.abs(dy);
    keepBadge.style.opacity = horizontal && dx > 0 ? Math.min(1, dx / 100) : 0;
    tossBadge.style.opacity = horizontal && dx < 0 ? Math.min(1, -dx / 100) : 0;
    skipBadge.style.opacity = !horizontal && dy < 0 ? Math.min(1, -dy / 100) : 0;
  }
  function onUp(e) {
    if (!dragging) return;
    dragging = false;
    try { card.releasePointerCapture(pointerId); } catch {}
    const THRESH = 100;
    const horizontal = Math.abs(dx) > Math.abs(dy);
    let decision = null;
    if (horizontal && dx > THRESH) decision = 'keep';
    else if (horizontal && dx < -THRESH) decision = 'toss';
    else if (!horizontal && dy < -THRESH) decision = 'skip';
    if (decision) {
      commitSwipe(card, item, decision, dx, dy);
    } else {
      card.style.transition = 'transform 0.25s ease';
      card.style.transform = '';
      keepBadge.style.opacity = 0;
      tossBadge.style.opacity = 0;
      skipBadge.style.opacity = 0;
    }
    dx = 0; dy = 0;
  }
  card.addEventListener('pointerdown', onDown);
  card.addEventListener('pointermove', onMove);
  card.addEventListener('pointerup', onUp);
  card.addEventListener('pointercancel', onUp);
}

async function commitSwipe(card, item, decision, dx, dy) {
  if (voteInFlight || !state.currentUser) return;
  voteInFlight = true;
  setActionsEnabled(false);
  const flyX = decision === 'keep' ? 1000 : decision === 'toss' ? -1000 : (dx || 0);
  const flyY = decision === 'skip' ? -1200 : (dy || 0);
  const rot = decision === 'keep' ? 20 : decision === 'toss' ? -20 : 0;
  card.style.transition = 'transform 0.35s ease, opacity 0.35s ease';
  card.style.transform = `translate(${flyX}px, ${flyY}px) rotate(${rot}deg)`;
  card.style.opacity = '0';
  try {
    const res = await api.vote(item.id, state.currentUser.id, decision);
    if (decision === 'skip') toast('Saved for later');
    if (res.outcome) {
      const o = res.outcome.outcome;
      if (o === 'kept') toast('Kept — decision reached');
      else if (o === 'tossed') toast('Tossed — decision reached');
    }
  } catch (e) {
    toast(e.message);
  } finally {
    voteInFlight = false;
    state.deck = state.deck.filter(i => i.id !== item.id);
    await refreshDeck();
  }
}

document.querySelectorAll('.act').forEach(btn => {
  btn.addEventListener('click', () => {
    if (voteInFlight) return;
    if (state.deck.length === 0) return;
    const decision = btn.dataset.act;
    const top = cardStack.querySelector('.card');
    if (!top) return;
    commitSwipe(top, state.deck[0], decision, 0, 0);
  });
});

// ---------- add ----------
const photoInput = document.getElementById('photo-input');
const photoPreview = document.getElementById('photo-preview');
const photoPlaceholder = document.getElementById('photo-placeholder');
const itemTitle = document.getElementById('item-title');
const itemNote = document.getElementById('item-note');
const submitBtn = document.getElementById('btn-submit-item');
const addStatus = document.getElementById('add-status');

photoInput.addEventListener('change', async e => {
  const file = e.target.files?.[0];
  if (!file) return;
  try {
    const resized = await resizeImage(file, 1200, 0.85);
    state.pendingPhoto = resized;
    photoPreview.src = URL.createObjectURL(resized);
    photoPreview.hidden = false;
    photoPlaceholder.hidden = true;
    submitBtn.disabled = false;
  } catch {
    toast('Could not load photo');
  }
});

async function resizeImage(file, maxDim, quality) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    const url = URL.createObjectURL(file);
    img.onload = () => {
      let { naturalWidth: w, naturalHeight: h } = img;
      if (w > maxDim || h > maxDim) {
        const ratio = Math.min(maxDim / w, maxDim / h);
        w = Math.round(w * ratio);
        h = Math.round(h * ratio);
      }
      const canvas = document.createElement('canvas');
      canvas.width = w; canvas.height = h;
      canvas.getContext('2d').drawImage(img, 0, 0, w, h);
      canvas.toBlob(b => {
        URL.revokeObjectURL(url);
        b ? resolve(b) : reject(new Error('blob failed'));
      }, 'image/jpeg', quality);
    };
    img.onerror = () => { URL.revokeObjectURL(url); reject(new Error('image load failed')); };
    img.src = url;
  });
}

submitBtn.addEventListener('click', async () => {
  if (!state.currentUser || !state.pendingPhoto) return;
  const ruleInput = document.querySelector('input[name="voting-rule"]:checked');
  const rule = ruleInput ? ruleInput.value : 'keep_wins';
  submitBtn.disabled = true;
  addStatus.hidden = false;
  addStatus.className = 'status';
  addStatus.textContent = 'Uploading...';
  try {
    await api.createItem(
      state.pendingPhoto,
      itemTitle.value.trim(),
      itemNote.value.trim(),
      state.currentUser.id,
      rule,
    );
    addStatus.textContent = 'Added to the pile!';
    addStatus.className = 'status ok';
    state.pendingPhoto = null;
    itemTitle.value = '';
    itemNote.value = '';
    photoInput.value = '';
    photoPreview.hidden = true;
    photoPlaceholder.hidden = false;
    const defaultRule = document.querySelector('input[name="voting-rule"][value="keep_wins"]');
    if (defaultRule) defaultRule.checked = true;
    setTimeout(() => { addStatus.hidden = true; showView('deck'); }, 900);
  } catch (e) {
    addStatus.textContent = e.message;
    addStatus.className = 'status fail';
    submitBtn.disabled = false;
  }
});

// ---------- ongoing ----------
async function refreshOngoing() {
  if (!state.currentHousehold) return;
  try {
    const all = await api.items(state.currentHousehold.id);
    state.ongoing = all.filter(i => i.status === 'pending');
    state.users = await api.listUsers(state.currentHousehold.id);
  } catch { state.ongoing = []; }
  const list = document.getElementById('ongoing-list');
  if (state.ongoing.length === 0) {
    list.innerHTML = '<div class="empty muted">Nothing is being voted on right now.</div>';
    return;
  }
  list.innerHTML = '';
  const total = state.users.length || 1;
  state.ongoing.forEach(item => {
    const addedBy = state.users.find(u => u.id === item.created_by)?.name || '?';
    const voted = item.vote_count || 0;
    const skipped = item.skip_count || 0;
    const pct = Math.min(100, Math.round((voted / total) * 100));
    const rulePill = item.voting_rule === 'majority'
      ? '<span class="rule-pill majority">majority</span>'
      : '<span class="rule-pill">keep wins</span>';
    const row = document.createElement('div');
    row.className = 'ongoing-row';
    const tally = item.tally || { keep: 0, toss: 0, skip: 0 };
    row.innerHTML = `
      <img src="/photos/${encodeURIComponent(item.photo_path)}" alt="">
      <div class="info">
        <div class="title">${escapeHtml(item.title || 'Untitled')}</div>
        <div class="sub">by ${escapeHtml(addedBy)} · ${rulePill}</div>
        <div class="progress-bar"><div style="width:${pct}%"></div></div>
        <div class="sub" style="margin-top:6px;">
          ${voted}/${total} voted
          · <span class="tally-pill">K ${tally.keep} · T ${tally.toss}${skipped ? ' · S ' + skipped : ''}</span>
        </div>
      </div>
    `;
    row.addEventListener('click', () => openItemDetail(item.id));
    list.appendChild(row);
  });
}

// ---------- history ----------
async function refreshHistory() {
  if (!state.currentHousehold) return;
  try {
    const all = await api.items(state.currentHousehold.id);
    state.history = all.filter(i => i.status !== 'pending');
    state.users = await api.listUsers(state.currentHousehold.id);
  } catch { state.history = []; }
  const list = document.getElementById('history-list');
  if (state.history.length === 0) {
    list.innerHTML = '<div class="empty muted">Nothing decided yet.</div>';
    return;
  }
  list.innerHTML = '';
  state.history.forEach(item => {
    const addedBy = state.users.find(u => u.id === item.created_by)?.name || '?';
    const date = new Date((item.decided_at || item.created_at) * 1000)
      .toLocaleString(undefined, { dateStyle: 'short', timeStyle: 'short' });
    const row = document.createElement('div');
    row.className = 'history-row';
    row.innerHTML = `
      <img src="/photos/${encodeURIComponent(item.photo_path)}" alt="">
      <div class="info">
        <div class="title">${escapeHtml(item.title || 'Untitled')}</div>
        <div class="sub">by ${escapeHtml(addedBy)} · ${date}</div>
      </div>
      <div class="outcome ${item.status}">${item.status}</div>
    `;
    row.addEventListener('click', () => openItemDetail(item.id));
    list.appendChild(row);
  });
}

// ---------- who ----------
async function refreshWho() {
  if (!state.currentHousehold) return;
  try {
    state.users = await api.listUsers(state.currentHousehold.id);
    state.stats = await api.stats(state.currentHousehold.id);
    // refresh household (name may have been renamed from another device)
    const hh = await api.getHousehold(state.currentHousehold.id);
    if (hh) {
      state.currentHousehold = hh;
      applyHeaderContext();
    }
  } catch {}
  document.getElementById('invite-code').textContent = state.currentHousehold.invite_code;
  const list = document.getElementById('users-list');
  list.innerHTML = '';
  if (state.users.length === 0) {
    list.innerHTML = '<div class="empty muted">No one yet.</div>';
  }
  state.users.forEach(u => {
    const isMe = state.currentUser && u.id === state.currentUser.id;
    const row = document.createElement('div');
    row.className = 'user-row' + (isMe ? ' you' : '');
    const joined = new Date(u.created_at * 1000).toLocaleDateString();
    row.innerHTML = `
      <div class="user-info">
        <div><b>${escapeHtml(u.name)}</b>${isMe ? ' <span class="muted">(you)</span>' : ''}</div>
        <div class="muted" style="font-size:12px">since ${joined}</div>
      </div>
      <button class="del-user-btn" type="button">Remove</button>
    `;
    row.querySelector('.del-user-btn').addEventListener('click', () => removeUserFlow(u));
    list.appendChild(row);
  });
  document.getElementById('stat-pending').textContent = state.stats.pending || 0;
  document.getElementById('stat-kept').textContent = state.stats.kept || 0;
  document.getElementById('stat-tossed').textContent = state.stats.tossed || 0;
}

async function removeUserFlow(user) {
  const isMe = state.currentUser && user.id === state.currentUser.id;
  const msg = isMe
    ? `Remove yourself from "${state.currentHousehold.name}"?`
    : `Remove ${user.name}? Their votes will be deleted. Items they added stay.`;
  if (!confirm(msg)) return;
  try {
    await api.deleteUser(user.id);
    toast(isMe ? 'You left' : `${user.name} removed`);
    if (isMe) {
      removeMembership(state.currentHousehold.id);
      state.currentHousehold = null;
      state.currentUser = null;
      const next = state.lastUsed || Object.keys(state.memberships)[0];
      if (next) { await switchToHousehold(next); } else { showView('lobby'); }
      return;
    }
    await refreshConfig();
    refreshWho();
  } catch (e) {
    toast(e.message);
  }
}

document.getElementById('btn-leave-household').addEventListener('click', () => {
  if (!state.currentUser) return;
  removeUserFlow(state.currentUser);
});

document.getElementById('btn-rename-household').addEventListener('click', async () => {
  if (!state.currentHousehold) return;
  const name = prompt('Rename household:', state.currentHousehold.name);
  if (!name || !name.trim()) return;
  try {
    const hh = await api.renameHousehold(state.currentHousehold.id, name.trim());
    state.currentHousehold = hh;
    applyHeaderContext();
    refreshWho();
    toast('Renamed');
  } catch (e) {
    toast(e.message);
  }
});

document.getElementById('btn-copy-code').addEventListener('click', async () => {
  if (!state.currentHousehold) return;
  const code = state.currentHousehold.invite_code;
  const shareText =
    `Join our Trash Tinder household "${state.currentHousehold.name}" with code: ${code}`;
  // Prefer native share sheet on mobile; fall back to clipboard.
  if (navigator.share) {
    try {
      await navigator.share({ title: 'Trash Tinder', text: shareText });
      return;
    } catch {
      // user cancelled — fall through to clipboard
    }
  }
  try {
    await navigator.clipboard.writeText(code);
    toast('Code copied');
  } catch {
    toast(`Code: ${code}`);
  }
});

document.getElementById('btn-clear-done').addEventListener('click', async () => {
  if (!state.currentHousehold) return;
  if (!confirm('Delete every already-decided item in this household? Photos are removed from disk. Pending items stay.')) return;
  try {
    const res = await api.clearDoneItems(state.currentHousehold.id);
    toast(`Deleted ${res.deleted} item${res.deleted === 1 ? '' : 's'}`);
    refreshWho();
  } catch (e) {
    toast(e.message);
  }
});

// ---------- item detail modal ----------
const modal = document.getElementById('item-modal');

async function openItemDetail(itemId) {
  state.selectedItemId = itemId;
  try {
    const item = await api.itemDetail(itemId);
    renderItemDetail(item);
    modal.hidden = false;
  } catch (e) {
    toast(e.message);
  }
}

function closeItemDetail() {
  modal.hidden = true;
  state.selectedItemId = null;
}

function renderItemDetail(item) {
  document.getElementById('modal-photo').src = '/photos/' + encodeURIComponent(item.photo_path);
  document.getElementById('modal-title').textContent = item.title || 'Untitled';
  const note = document.getElementById('modal-note');
  note.textContent = item.note || '';
  note.hidden = !item.note;
  const addedBy = state.users.find(u => u.id === item.created_by)?.name || '(removed)';
  const createdAt = new Date(item.created_at * 1000)
    .toLocaleString(undefined, { dateStyle: 'short', timeStyle: 'short' });
  document.getElementById('modal-meta').textContent = `Added by ${addedBy} · ${createdAt}`;
  document.getElementById('modal-rule').textContent =
    item.voting_rule === 'majority'
      ? 'Rule: majority wins (ties go to kept)'
      : 'Rule: any keep saves it';

  const statusRow = document.getElementById('modal-status-row');
  statusRow.innerHTML = '';
  const chip = document.createElement('div');
  if (item.status === 'pending') {
    chip.className = 'vote-chip none';
    chip.textContent = 'pending';
  } else {
    chip.className = 'outcome ' + item.status;
    chip.textContent = item.status;
  }
  statusRow.appendChild(chip);

  const votesEl = document.getElementById('modal-votes');
  votesEl.innerHTML = '';
  (item.votes || []).forEach(v => {
    const row = document.createElement('div');
    row.className = 'vote-row' + (v.user_name === '(removed)' ? ' removed' : '');
    const chipClass = v.choice || 'none';
    const chipText = v.choice ? v.choice.toUpperCase() : 'NOT YET';
    row.innerHTML = `
      <div class="vote-name">${escapeHtml(v.user_name)}</div>
      <div class="vote-chip ${chipClass}">${chipText}</div>
    `;
    votesEl.appendChild(row);
  });

  const myVoteEl = document.getElementById('modal-my-vote');
  const myHint = document.getElementById('modal-my-vote-hint');
  if (item.status === 'pending' && state.currentUser) {
    myVoteEl.hidden = false;
    const me = (item.votes || []).find(v => v.user_id === state.currentUser.id);
    const myChoice = me ? me.choice : null;
    myVoteEl.querySelectorAll('.my-vote-btn').forEach(btn => {
      btn.classList.toggle('selected', btn.dataset.choice === myChoice);
    });
    myHint.textContent = myChoice
      ? `You voted ${myChoice}. Tap another to change it.`
      : `You haven't voted yet. Tap one to cast.`;
  } else {
    myVoteEl.hidden = true;
  }

  const delBtn = document.getElementById('modal-delete');
  delBtn.hidden = false;
  delBtn.textContent = item.status === 'pending'
    ? 'Delete this item (cancel voting)'
    : 'Delete this item';
}

async function changeMyVote(choice) {
  if (!state.selectedItemId || !state.currentUser) return;
  try {
    const res = await api.vote(state.selectedItemId, state.currentUser.id, choice);
    if (res.outcome) {
      const o = res.outcome.outcome;
      toast(o === 'kept' ? 'Decision: kept' : 'Decision: tossed');
    } else if (choice === 'skip') {
      toast('Saved for later');
    } else {
      toast(`Your vote: ${choice}`);
    }
    const fresh = await api.itemDetail(state.selectedItemId);
    renderItemDetail(fresh);
    if (state.view === 'ongoing') refreshOngoing();
    if (state.view === 'deck') refreshDeck();
    if (state.view === 'history') refreshHistory();
  } catch (e) {
    toast(e.message);
  }
}

document.querySelectorAll('#modal-my-vote .my-vote-btn').forEach(btn => {
  btn.addEventListener('click', () => changeMyVote(btn.dataset.choice));
});

document.getElementById('modal-close').addEventListener('click', closeItemDetail);
document.getElementById('modal-backdrop').addEventListener('click', closeItemDetail);
document.getElementById('modal-delete').addEventListener('click', async () => {
  if (!state.selectedItemId) return;
  if (!confirm('Delete this item and its photo? This cannot be undone.')) return;
  try {
    await api.deleteItem(state.selectedItemId);
    toast('Item deleted');
    closeItemDetail();
    if (state.view === 'history') refreshHistory();
    if (state.view === 'ongoing') refreshOngoing();
    if (state.view === 'deck') refreshDeck();
  } catch (e) {
    toast(e.message);
  }
});

// ---------- polling (replaces SSE for WSGI / PythonAnywhere compat) ----------
let pollTimer = null;
function startPolling() {
  stopPolling();
  pollTimer = setInterval(() => {
    if (document.hidden) return;
    if (modal && !modal.hidden) return; // don't refresh while reading detail
    if (state.view === 'deck') refreshDeck();
    else if (state.view === 'ongoing') refreshOngoing();
    else if (state.view === 'history') refreshHistory();
    else if (state.view === 'who') { refreshConfig(); refreshWho(); }
  }, 3000);
}
function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

// ---------- boot ----------
async function boot() {
  const { memberships, lastUsed } = loadMemberships();
  state.memberships = memberships;
  state.lastUsed = lastUsed;

  const hhIds = Object.keys(memberships);
  if (hhIds.length === 0) {
    showView('lobby');
    return;
  }

  const target = (lastUsed && memberships[lastUsed]) ? lastUsed : hhIds[0];
  const ok = await switchToHousehold(target);
  if (ok) return;

  // All memberships were stale — go back to lobby.
  if (Object.keys(state.memberships).length === 0) {
    showView('lobby');
    return;
  }
  // Try another one
  for (const id of Object.keys(state.memberships)) {
    if (await switchToHousehold(id)) return;
  }
  showView('lobby');
}

if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(() => {});
}

boot();
