// 検索UI用：キャラ選択・編成・バリデーション・結果表示
const atkLabels = ["A1", "A2", "A3", "A4", "SP", "SP"];
const defLabels = ["D1", "D2", "D3", "D4", "SP", "SP"];
let atkOrDef = "防衛";
let defenseTeam = [null, null, null, null, null, null];
let currSlot = null;

function renderDefenseRow() {
  const row = document.getElementById("defenseRow");
  row.innerHTML = "";
  defenseTeam.forEach((ch, idx) => {
    const slot = document.createElement("div");
    slot.className = "def-slot";
    slot.onclick = () => openModal(idx);
    if (ch) {
      slot.innerHTML = `<img src="${ch.image}" alt="${ch.name}"><button class="remove-icon" onclick="removeChar(event,${idx})">&times;</button>`;
    } else {
      slot.innerHTML = `<span style="opacity:0.6;">EMPTY</span>`;
    }
    row.appendChild(slot);
  });

  // ラベルを下行で別に表示
  const labelRow = document.getElementById("slotLabelRow");
  labelRow.innerHTML = "";
  const slotLabels = atkOrDef === "攻撃" ? atkLabels : defLabels;
  slotLabels.forEach(label => {
    const div = document.createElement("div");
    div.className = "slot-label";
    div.textContent = label;
    labelRow.appendChild(div);
  });
}
window.removeChar = function(e, idx) {
  e.stopPropagation();
  defenseTeam[idx] = null;
  renderDefenseRow();
};

// キャラ選択モーダル
function openModal(idx) {
  currSlot = idx;
  const all = strikerList.concat(specialList);
  let charOptions = all.map((c, i) =>
    `<button onclick="selectChar(${i})" style="margin:2px 3px 2px 0;padding:3px 7px;">
      <img src="${c.image}" alt="${c.name}" style="width:32px;height:32px;">${c.name}
    </button>`
  ).join("");
  let modalDiv = document.createElement("div");
  modalDiv.id = "charModal";
  modalDiv.style = "position:fixed;top:0;left:0;width:100vw;height:100vh;background:rgba(0,0,0,0.38);z-index:10000;display:flex;align-items:center;justify-content:center;";
  modalDiv.innerHTML = `<div style="background:#fff;border-radius:15px;padding:25px 18px;max-width:600px;">
      <div style="font-size:1.1em;margin-bottom:8px;">キャラを選択してください</div>
      <div style="display:flex;flex-wrap:wrap;max-height:310px;overflow:auto;">${charOptions}</div>
      <div style="text-align:right;margin-top:12px;"><button onclick="closeModal()">キャンセル</button></div>
    </div>`;
  document.body.appendChild(modalDiv);
  window.selectChar = function(i) {
    defenseTeam[currSlot] = strikerList.concat(specialList)[i];
    closeModal();
    renderDefenseRow();
  };
  window.closeModal = function() {
    let m = document.getElementById("charModal");
    if (m) m.remove();
  };
}

// 攻撃/防衛切り替え
document.getElementById("atkTab").onclick = () => {
  atkOrDef = "攻撃";
  document.getElementById("atkTab").classList.add("active");
  document.getElementById("defTab").classList.remove("active");
  renderDefenseRow();
};
document.getElementById("defTab").onclick = () => {
  atkOrDef = "防衛";
  document.getElementById("defTab").classList.add("active");
  document.getElementById("atkTab").classList.remove("active");
  renderDefenseRow();
};

// 検索ボタンのイベント（バリデーション付き！）
document.querySelector(".search-btn").onclick = async () => {
  const charNames = defenseTeam.map(c => c ? c.name : "");
  if (charNames.filter(x => !!x).length === 0) {
    alert("最低1キャラ以上選択してください。");
    return;
  }
  const side = atkOrDef === "攻撃" ? "attack" : "defense";
  const res = await fetch("/api/search", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({side: side, characters: charNames})
  });
  const data = await res.json();
  showSearchResults(data.results || []);
};

// 検索結果表示
function showSearchResults(results) {
  const section = document.getElementById("searchResultsSection");
  const container = document.getElementById("searchResults");
  section.style.display = results.length > 0 ? "block" : "none";
  container.innerHTML = "";
  if (results.length === 0) {
    container.innerHTML = "<div style='color:#888;font-size:1.05em;'>該当するログはありませんでした。</div>";
    return;
  }
  results.forEach(res => {
    const winnerChars = res.winner_characters.map(name => charImageTag(name));
    const loserChars  = res.loser_characters.map(name => charImageTag(name));
    const winnerIcon = res.winner_icon ? `<img class="side-icon" src="${res.winner_icon}" alt="side">` : "";
    const loserIcon  = res.loser_icon  ? `<img class="side-icon" src="${res.loser_icon}" alt="side">` : "";
    const dateTag = res.date ? `<div class="result-date">${res.date}</div>` : "";
    container.innerHTML += `
      <div class="result-row">
        <div class="side-col">
          ${winnerIcon}
          <div class="char-row">${winnerChars.join('')}</div>
          <div style="font-weight:bold;color:#2288aa;">勝ち</div>
          ${dateTag}
        </div>
        <div class="vs-mark">VS</div>
        <div class="side-col">
          ${loserIcon}
          <div class="char-row">${loserChars.join('')}</div>
          <div style="font-weight:bold;color:#aa3333;">負け</div>
          ${dateTag}
        </div>
      </div>
    `;
  });
}
function charImageTag(name) {
  if (!name) return `<span style="width:36px;height:36px;display:inline-block;"></span>`;
  let all = strikerList.concat(specialList);
  let found = all.find(c => c.name === name);
  if (found && found.image) {
    return `<img class="char-img" src="${found.image}" alt="${name}">`;
  } else {
    return `<span class="char-img" style="background:#eee;border:1px solid #ddd;"></span>`;
  }
}

// 初期表示
renderDefenseRow();
