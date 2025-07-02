// 背景画像をランダムに設定（画像は background_img フォルダに保存）
// 必要に応じてファイル名を増やしてください
const bgImages = [
  "bg1.png",
  "bg2.png",
  "bg3.png"
];

const randomBg = bgImages[Math.floor(Math.random() * bgImages.length)];
document.body.style.backgroundImage = `url('/static/background_img/${randomBg}')`;

// 画像選択時にプレビュー表示（送信処理は止めない！）
document.getElementById("imageInput").addEventListener("change", function () {
  const file = this.files[0];
  if (!file) return;

  const reader = new FileReader();
  reader.onload = function (event) {
    const img = document.getElementById("preview-image");
    img.src = event.target.result;
    document.getElementById("preview-section").classList.remove("d-none");
  };
  reader.readAsDataURL(file);
});



// --- 共通ナビ用JS（script.jsに追加） ---
const hamburger = document.getElementById('hamburger-btn');
const sidebar = document.getElementById('sidebar');
const sidebarBg = document.getElementById('sidebar-bg');
const closeSidebar = document.getElementById('close-sidebar');

hamburger.addEventListener('click', () => {
  sidebar.classList.add('active');
  sidebarBg.style.display = 'block';
});
sidebarBg.addEventListener('click', () => {
  sidebar.classList.remove('active');
  sidebarBg.style.display = 'none';
});
closeSidebar.addEventListener('click', () => {
  sidebar.classList.remove('active');
  sidebarBg.style.display = 'none';
});
