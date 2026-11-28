import "./app.js";
import { dateLabel, deleteChat, listChats } from "./chats.js";

const list = document.querySelector("#chat-list");
const count = document.querySelector("#chat-count");
const retention = document.querySelector("#chat-retention");
const notice = document.querySelector("#chat-notice");
const deletion = document.querySelector("#chat-delete");
const limitDialog = document.querySelector("#chat-limit");
const limitOptions = document.querySelector("#chat-limit-options");
const limitConfirm = document.querySelector("#confirm-chat-limit");
let selectedChat;
let deletingChats = false;
const query = new URLSearchParams(location.search);
const instagramSection = query.get("section") === "instagram" && !query.has("new");
const previewLimit = query.get("preview") === "chat-limit";
const preview = (query.get("preview") === "chats" || previewLimit) && !query.has("new");
const previewRetention = 120 * 3600;
let sampleChats = preview ? [
  ["원컷 네일샵", "2026-11-28T16:20:00+09:00"],
  ["모모 네일", "2026-11-27T14:05:00+09:00"],
  ["라운드 네일", "2026-11-26T11:40:00+09:00"],
  ...(previewLimit ? [
    ["데일리 네일", "2026-11-25T15:10:00+09:00"],
    ["시럽 네일", "2026-11-24T13:30:00+09:00"],
  ] : []),
].map(([shop, timestamp], index) => {
  const updatedAt = Date.parse(timestamp);
  return { id: `sample-${index}`, fields: { shop }, updatedAt, expiresAt: updatedAt + previewRetention * 1000 };
}) : [];

// 매장명으로 채팅 제목을 표시하고 빈 제목에는 기본 문구를 사용합니다.
function title(chat) {
  return chat.fields.shop || "제목 없는 광고";
}

// 채팅의 수정 시각, 삭제 예정 시각, 관리 버튼을 목록 행으로 만듭니다.
function createRow(chat) {
  const row = document.createElement("li");
  row.innerHTML = '<div><strong></strong><p class="chat-dates"><span></span><span></span></p></div><div class="workflow-actions"><a class="button button-outline">이어서 수정</a><button class="button button-plain chat-remove" type="button">삭제</button></div>';
  row.querySelector("strong").textContent = title(chat);
  const [updated, expires] = row.querySelectorAll(".chat-dates span");
  updated.textContent = `마지막 수정일 ${dateLabel(chat.updatedAt)}`;
  expires.textContent = `삭제 예정일 ${dateLabel(chat.expiresAt)}`;
  const resume = row.querySelector("a");
  if (preview) resume.setAttribute("aria-disabled", "true");
  else resume.href = `./create.html?chat=${encodeURIComponent(chat.id)}`;
  row.querySelector("button").addEventListener("click", () => {
    selectedChat = chat.id;
    deletion.showModal();
  });
  return row;
}

// 서버에서 채팅 목록과 보관 정책을 받아 마이페이지에 표시합니다.
async function renderChats() {
  try {
    const { chats, chatLimit, retentionSeconds } = preview
      ? { chats: sampleChats, chatLimit: 5, retentionSeconds: previewRetention }
      : await listChats();
    count.textContent = `사용 중인 채팅 ${chats.length}/${chatLimit}`;
    retention.textContent = `마지막 수정 시각부터 ${retentionSeconds / 3600}시간 동안 보관 · 같은 계정으로 이어서 수정 가능`;
    list.replaceChildren(...chats.map(createRow));
    notice.textContent = chats.length ? "" : "채팅 내역이 없습니다.";
  } catch {
    notice.textContent = "채팅 내역을 불러오지 못했습니다.";
  }
}

// 선택한 채팅을 서버에서 삭제하고 실패하면 안내를 표시합니다.
async function removeChat(id) {
  try {
    if (preview) sampleChats = sampleChats.filter((chat) => chat.id !== id);
    else await deleteChat(id);
    return true;
  } catch {
    notice.textContent = "채팅을 삭제하지 못했습니다. 다시 시도해 주세요.";
    return false;
  }
}

deletion.addEventListener("close", async () => {
  if (deletion.returnValue === "delete" && await removeChat(selectedChat)) await renderChats();
  deletion.returnValue = "";
});

// 채팅 저장 한도를 확인해 생성 화면이나 기존 채팅 삭제 팝업을 엽니다.
async function openNewChat() {
  if (deletingChats) return;
  try {
    const { chats, chatLimit } = preview ? { chats: sampleChats, chatLimit: 5 } : await listChats();
    if (chats.length < chatLimit) {
      location.assign("./create.html");
      return;
    }
    const options = chats.map((chat) => {
      const label = document.createElement("label");
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.name = "chat";
      checkbox.value = chat.id;
      label.append(checkbox, `${title(chat)} · 삭제 예정 ${dateLabel(chat.expiresAt)}`);
      return label;
    });
    limitOptions.querySelectorAll("label").forEach((label) => label.remove());
    limitOptions.append(...options);
    limitConfirm.disabled = true;
    document.querySelector("#chat-limit-title").textContent = `저장된 채팅이 ${chatLimit}개 있습니다`;
    limitDialog.showModal();
  } catch {
    notice.textContent = "채팅을 확인하지 못했습니다. 다시 시도해 주세요.";
  }
}

document.querySelector("#new-chat").addEventListener("click", (event) => {
  event.preventDefault();
  void openNewChat();
});
document.querySelector("#cancel-limit").addEventListener("click", () => limitDialog.close());
limitOptions.addEventListener("change", () => {
  limitConfirm.disabled = !limitOptions.querySelector("input:checked");
});
document.querySelector("#chat-limit-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const ids = new FormData(event.currentTarget).getAll("chat");
  if (deletingChats || !ids.length) return;
  deletingChats = true;
  limitDialog.close();
  let failed = 0;
  for (const id of ids) if (!await removeChat(id)) failed += 1;
  if (!failed) {
    location.assign("./create.html");
    return;
  }
  await renderChats();
  notice.textContent = `선택한 채팅 중 ${failed}개를 삭제하지 못했습니다. 다시 시도해 주세요.`;
  deletingChats = false;
});

if (instagramSection) {
  document.title = "인스타그램 계정 | 원컷 네일";
  document.querySelector("#chat-history-menu").removeAttribute("aria-current");
  document.querySelector("#instagram-account-menu").setAttribute("aria-current", "page");
  document.querySelector("#chat-history-content").hidden = true;
  document.querySelector("#instagram-account-content").hidden = false;
  const connected = document.querySelector("#instagram-connected");
  const disconnected = document.querySelector("#instagram-disconnected");
  if (query.get("preview") === "instagram-connected") {
    connected.hidden = false;
    disconnected.hidden = true;
  }
  document.querySelector("#disconnect-instagram-preview").addEventListener("click", () => {
    connected.hidden = true;
    disconnected.hidden = false;
  });
} else {
  document.addEventListener("onecut-authenticated", renderChats);
  void renderChats();
}

if (query.has("new")) {
  history.replaceState(null, "", "./mypage.html");
  void openNewChat();
}
