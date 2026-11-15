'use strict';
const commands = { setup: '-m starter.local doctor\n{python} -m starter.local setup\n{python} -m starter.local up', client: '-m starter.client', restart: '-m starter.local stop\n{python} -m starter.local start\n{python} -m starter.local backup' };
const platform = document.querySelector('#platform');
const status = document.querySelector('#copy-status');
platform.addEventListener('change', () => {
  const python = { unix: 'python3', windows: 'py -3.12', python: 'python' }[platform.value];
  for (const [id, command] of Object.entries(commands)) document.querySelector(`#${id} code`).textContent = `${python} ${command.replaceAll('{python}', python)}`;
  status.textContent = '';
});
document.querySelectorAll('[data-copy]').forEach(button => button.addEventListener('click', async () => {
  const text = document.getElementById(button.dataset.copy).textContent;
  try { await navigator.clipboard.writeText(text); status.textContent = 'Commands copied.'; button.textContent = 'Copied'; }
  catch { status.textContent = 'Clipboard unavailable. Select and copy the visible commands.'; }
  setTimeout(() => { button.textContent = 'Copy'; }, 1800);
}));
