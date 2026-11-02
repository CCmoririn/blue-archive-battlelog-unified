"use strict";
document.querySelectorAll("form").forEach(form => {
  form.addEventListener("submit", event => {
    if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) {
      event.preventDefault(); return;
    }
    const button = event.submitter;
    if (button) { button.disabled = true; button.textContent = "処理しています…"; }
  });
});
const search = document.getElementById("character-search");
const filter = document.getElementById("kind-filter");
function filterCharacters() {
  const word = search.value.normalize("NFKC").toLowerCase();
  document.querySelectorAll("#character-list tr").forEach(row => {
    row.hidden = !row.dataset.name.normalize("NFKC").toLowerCase().includes(word) || (filter.value && row.dataset.kind !== filter.value);
  });
}
if (search && filter) { search.addEventListener("input",filterCharacters); filter.addEventListener("change",filterCharacters); }
const kind = document.getElementById("character-kind");
if (kind) kind.addEventListener("change", () => { document.getElementById("striker-fields").hidden = kind.value === "SPECIAL"; });
const iconUrl = document.getElementById("icon-url");
const preview = document.getElementById("icon-preview");
const previewMessage = document.getElementById("preview-message");
if (iconUrl && preview) {
  if (preview.complete && preview.naturalWidth > 0) previewMessage.textContent = "画像を確認できました。";
  preview.addEventListener("load", () => { previewMessage.textContent = "画像を確認できました。"; });
  preview.addEventListener("error", () => { preview.hidden = true; previewMessage.textContent = "画像を表示できません。URLや共有設定を確認してください。"; });
  iconUrl.addEventListener("change", () => {
    try { const url = new URL(iconUrl.value); if(url.protocol !== "https:") throw Error(); preview.hidden = false; preview.src = url.href; }
    catch { preview.hidden = true; previewMessage.textContent = "https:// で始まる画像URLを入力してください。"; }
  });
}
