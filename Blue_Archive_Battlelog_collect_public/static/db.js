// GOOD/BADの状態はシーズンごとに分け、保存に成功したときだけ画面へ反映する。
function setCookie(name, value, days = 180) {
    const expires = new Date(Date.now() + days * 86400000).toUTCString();
    document.cookie = `${name}=${value};expires=${expires};path=/;SameSite=Lax`;
}
function getCookie(name) {
    const prefix = `${name}=`;
    const cookie = document.cookie.split("; ").find(value => value.startsWith(prefix));
    return cookie ? cookie.slice(prefix.length) : null;
}
function deleteCookie(name) {
    document.cookie = `${name}=;expires=Thu, 01 Jan 1970 00:00:00 GMT;path=/;SameSite=Lax`;
}
function voteCookie(btn) {
    return `voted_v2_${btn.dataset.season}_${btn.dataset.type}_${btn.dataset.rowId}`;
}
function renderVoteButtons() {
    document.querySelectorAll(".vote-btn").forEach(btn => {
        const voted = getCookie(voteCookie(btn)) === "1";
        btn.classList.toggle("voted", voted);
        btn.title = voted ? "もう一度押すと取り消せます" : "";
    });
}
async function sendVote(rowId, type, currentStatus, season) {
    try {
        const response = await fetch("/api/vote", {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({row_id: rowId, vote_type: type, current_status: currentStatus, season})
        });
        return await response.json();
    } catch (error) {
        return {success: false, message: "評価結果を確認できませんでした。ページを再読み込みしてください。"};
    }
}
function setupVoteHandlers() {
    document.querySelectorAll(".vote-btn").forEach(btn => {
        if (btn.dataset.bound) return;
        btn.dataset.bound = "1";
        btn.addEventListener("click", async () => {
            const {rowId, type, season} = btn.dataset;
            if (!season || !rowId || btn.disabled) return;
            const buttons = Array.from(document.querySelectorAll(".vote-btn")).filter(other => other.dataset.rowId === rowId);
            buttons.forEach(other => { other.disabled = true; });
            const voted = getCookie(voteCookie(btn)) === "1";
            try {
                const result = await sendVote(rowId, type, voted, season);
                if (!result || !result.success) {
                    alert(result?.message || "評価できませんでした。ページを再読み込みしてください。");
                    return;
                }
                if (voted) deleteCookie(voteCookie(btn));
                else setCookie(voteCookie(btn), "1");
                document.querySelectorAll(".result-row").forEach(row => {
                    if (row.dataset.rowId !== rowId) return;
                    row.dataset.good = String(result.good_count);
                    row.dataset.bad = String(result.bad_count);
                    row.querySelector(`#count_good_${rowId}`).textContent = result.good_count;
                    row.querySelector(`#count_bad_${rowId}`).textContent = result.bad_count;
                });
                renderVoteButtons();
                const select = document.getElementById("sortSelect");
                if (select) sortResults(select.value);
            } finally {
                buttons.forEach(other => { other.disabled = false; });
            }
        });
    });
}
function setupSortHandlers() {
    const select = document.getElementById("sortSelect");
    if (!select) return;
    if (!select.dataset.bound) {
        select.addEventListener("change", () => sortResults(select.value));
        select.dataset.bound = "1";
    }
    sortResults(select.value);
}
function sortResults(key) {
    const container = document.getElementById("searchResults");
    if (!container) return;
    const rows = Array.from(container.querySelectorAll(".result-row"));
    rows.sort((a, b) => {
        const ga = Number(a.dataset.good || 0), ba = Number(a.dataset.bad || 0);
        const gb = Number(b.dataset.good || 0), bb = Number(b.dataset.bad || 0);
        const ra = ga + ba ? ga / (ga + ba) : 0;
        const rb = gb + bb ? gb / (gb + bb) : 0;
        if (key === "rate") return rb - ra || gb - ga || Number(a.dataset.order) - Number(b.dataset.order);
        if (key === "good") return gb - ga || rb - ra || Number(a.dataset.order) - Number(b.dataset.order);
        return Number(a.dataset.order) - Number(b.dataset.order);
    });
    rows.forEach(row => container.appendChild(row));
}
window.addEventListener("DOMContentLoaded", () => {
    renderVoteButtons();
    setupVoteHandlers();
    setupSortHandlers();
});
