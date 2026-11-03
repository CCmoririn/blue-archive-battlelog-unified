"use strict";
function fullwidthParens(field) {
  const start = field.selectionStart, end = field.selectionEnd;
  const fixed = field.value.replace(/\(/g, "（").replace(/\)/g, "）");
  if (fixed !== field.value) {
    field.value = fixed;
    if (start !== null) field.setSelectionRange(start, end);
  }
}
document.querySelectorAll("[data-fullwidth-parens]").forEach(field => {
  field.addEventListener("input", event => { if (!event.isComposing) fullwidthParens(field); });
  field.addEventListener("compositionend", () => fullwidthParens(field));
  field.addEventListener("blur", () => fullwidthParens(field));
});
document.querySelectorAll("form").forEach(form => {
  form.addEventListener("submit", event => {
    form.querySelectorAll("[data-fullwidth-parens]").forEach(fullwidthParens);
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
const noticeDate = document.getElementById("notice-date");
const noticeText = document.getElementById("notice-text");
if (noticeDate && noticeText) {
  const update = () => {
    document.getElementById("notice-preview-date").textContent = noticeDate.value.replace(/-/g, "/");
    document.getElementById("notice-preview-text").textContent = noticeText.value;
  };
  noticeDate.addEventListener("input", update);
  noticeText.addEventListener("input", update);
  update();
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
