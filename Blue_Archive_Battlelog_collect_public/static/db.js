// ========== グッド・バッド投票ボタン管理JS ==========
// ご主人へ：このjsは <script src="/static/db.js"></script> でdb.html等から読み込んでね

// ----------- Cookie操作ユーティリティ -----------

function setCookie(name, value, days = 180) {
    const d = new Date();
    d.setTime(d.getTime() + days*24*60*60*1000);
    document.cookie = `${name}=${value};expires=${d.toUTCString()};path=/`;
}
function getCookie(name) {
    const cookies = document.cookie.split("; ");
    for (const cookie of cookies) {
        const [key, val] = cookie.split("=");
        if (key === name) return val;
    }
    return null;
}
function deleteCookie(name) {
    document.cookie = `${name}=; expires=Thu, 01 Jan 1970 00:00:00 UTC; path=/;`;
}

// ----------- 投票ボタン描画・状態管理 -----------

function renderVoteButtons() {
    document.querySelectorAll(".vote-btn").forEach(btn => {
        const rowId = btn.dataset.rowId;
        const type = btn.dataset.type; // "good" or "bad"
        const cookieName = `voted_${type}_${rowId}`;
        const voted = getCookie(cookieName) === "1";
        btn.classList.toggle("voted", voted);
        btn.disabled = false;
        btn.title = voted ? "もう一度押すと取り消せます" : "";
    });
}

// ----------- 投票API呼び出し -----------

async function sendVote(rowId, type, currentStatus, season=null) {
    try {
        const res = await fetch("/api/vote", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({
                row_id: rowId,
                vote_type: type,
                action: "toggle",
                current_status: currentStatus,
                season: season,
            })
        });
        return await res.json();
    } catch (e) {
        alert("サーバー通信エラー（投票できませんでした）");
        return null;
    }
}

// ----------- 投票クリックハンドラ -----------

function setupVoteHandlers() {
    document.querySelectorAll(".vote-btn").forEach(btn => {
        btn.addEventListener("click", async function() {
            const rowId = btn.dataset.rowId;
            const type = btn.dataset.type;
            const countSpan = document.getElementById(`count_${type}_${rowId}`);
            const cookieName = `voted_${type}_${rowId}`;
            const voted = getCookie(cookieName) === "1";
            const season = btn.dataset.season || null;

            // 楽観的UI：即時反映
            let currentCount = parseInt(countSpan.innerText || "0");
            if (!voted) {
                countSpan.innerText = currentCount + 1;
                setCookie(cookieName, "1");
            } else {
                countSpan.innerText = Math.max(0, currentCount - 1);
                deleteCookie(cookieName);
            }
            btn.classList.toggle("voted", !voted);
            btn.disabled = true; // 二重押し防止

            // サーバーに送信
            const result = await sendVote(rowId, type, voted, season);
            btn.disabled = false;
            if (result && result.success) {
                // 最新値で上書き
                document.getElementById(`count_good_${rowId}`).innerText = result.good_count;
                document.getElementById(`count_bad_${rowId}`).innerText = result.bad_count;
            } else if(result && !result.success) {
                alert("投票エラー：" + (result.message || ""));
            }
        });
    });
}

// ----------- ソート処理 -----------

function setupSortHandlers() {
    const sortSelect = document.getElementById("sortSelect");
    if (!sortSelect) return;
    sortSelect.addEventListener("change", function() {
        sortResults(sortSelect.value);
    });
    // 初期ソート
    sortResults(sortSelect.value);
}

function sortResults(sortKey) {
    const container = document.getElementById("searchResults");
    if (!container) return;
    const rows = Array.from(container.querySelectorAll(".result-row"));
    rows.sort((a, b) => {
        const gA = parseInt(a.dataset.good || "0");
        const bA = parseInt(a.dataset.bad || "0");
        const gB = parseInt(b.dataset.good || "0");
        const bB = parseInt(b.dataset.bad || "0");
        // 比率（グッド率順）か数（グッド数順）か
        if (sortKey === "rate") {
            const rA = (gA + bA) > 0 ? gA / (gA + bA) : 0;
            const rB = (gB + bB) > 0 ? gB / (gB + bB) : 0;
            if (rA !== rB) return rB - rA;
            return gB - gA;
        } else if (sortKey === "good") {
            if (gA !== gB) return gB - gA;
            // 同数ならグッド率
            const rA = (gA + bA) > 0 ? gA / (gA + bA) : 0;
            const rB = (gB + bB) > 0 ? gB / (gB + bB) : 0;
            return rB - rA;
        } else {
            // デフォルトは「新着順」なので何もしない
            return 0;
        }
    });
    // 並べ替え反映
    rows.forEach(row => container.appendChild(row));
}

// ----------- 初期化 -----------

window.addEventListener("DOMContentLoaded", () => {
    renderVoteButtons();
    setupVoteHandlers();
    setupSortHandlers();
});
