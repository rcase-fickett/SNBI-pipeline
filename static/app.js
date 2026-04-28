// Shared utilities for all pages

async function api(url, body) {
  try {
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    return await res.json();
  } catch (e) {
    return { ok: false, error: e.message };
  }
}

function toast(msg, type) {
  const c = document.getElementById('toasts');
  if (!c) return;
  const t = document.createElement('div');
  t.className = `toast toast-${type === 'err' ? 'err' : 'ok'}`;
  t.textContent = msg;
  c.appendChild(t);
  setTimeout(() => t.remove(), 2800);
}

// Restore reviewer name in nav on every page
window.addEventListener('DOMContentLoaded', () => {
  const el = document.getElementById('reviewer-display');
  if (el) {
    const name = localStorage.getItem('snbi_reviewer');
    if (name) el.textContent = `Reviewer: ${name}`;
  }
});
