import "./app.js";
import { dateLabel, deleteChat, listChats } from "./chats.js";

const list = document.querySelector("#chat-list");
const count = document.querySelector("#chat-count");
const retention = document.querySelector("#chat-retention");
const notice = document.querySelector("#chat-notice");
const deletion = document.querySelector("#chat-delete");
const limitDialog = document.querySelector("#chat-limit");
const limitOptions = document.querySelector("#chat-limit-options");
let selectedChat;

// 매장명으로 채팅 제목을 표시하고 빈 제목에는 기본 문구를 사용합니다.
function title(chat) {
  return chat.fields.shop || "제목 없는 광고";
}

// 채팅의 수정 시각, 삭제 예정 시각, 관리 버튼을 목록 행으로 만듭니다.
function createRow(chat) {
  const row = document.createElement("li");
  row.innerHTML = '<div><strong></strong><p></p></div><div class="workflow-actions"><a class="button button-outline">이어서 수정</a><button class="button button-plain" type="button">삭제</button></div>';
  row.querySelector("strong").textContent = title(chat);
  row.querySelector("p").textContent = `수정 ${dateLabel(chat.updatedAt)} · 삭제 예정 ${dateLabel(chat.expiresAt)}`;
  row.querySelector("a").href = `./create.html?chat=${encodeURIComponent(chat.id)}`;
  row.querySelector("button").addEventListener("click", () => {
    selectedChat = chat.id;
    deletion.showModal();
  });
  return row;
}

// 서버에서 채팅 목록과 보관 정책을 받아 마이페이지에 표시합니다.
async function renderChats() {
  try {
    const { chats, chatLimit, retentionSeconds } = await listChats();
    count.textContent = `사용 중인 채팅 ${chats.length}/${chatLimit}`;
    retention.textContent = `마지막 수정 시각부터 ${retentionSeconds / 3600}시간 동안 보관 · 같은 계정으로 이어서 수정 가능`;
    list.replaceChildren(...chats.map(createRow));
    notice.textContent = chats.length ? "" : "저장된 채팅이 없습니다.";
  } catch {
    notice.textContent = "채팅 내역을 불러오지 못했습니다.";
  }
}

// 선택한 채팅을 서버에서 삭제하고 실패하면 안내를 표시합니다.
async function removeChat(id) {
  try {
    await deleteChat(id);
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
  try {
    const { chats, chatLimit } = await listChats();
    if (chats.length < chatLimit) {
      location.assign("./create.html");
      return;
    }
    const options = chats.map((chat) => {
      const label = document.createElement("label");
      const radio = document.createElement("input");
      radio.type = "radio";
      radio.name = "chat";
      radio.value = chat.id;
      radio.required = true;
      label.append(radio, `${title(chat)} · 삭제 예정 ${dateLabel(chat.expiresAt)}`);
      return label;
    });
    limitOptions.querySelectorAll("label").forEach((label) => label.remove());
    limitOptions.append(...options);
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
document.querySelector("#chat-limit-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const id = new FormData(event.currentTarget).get("chat");
  limitDialog.close();
  if (await removeChat(id)) location.assign("./create.html");
});

if (new URLSearchParams(location.search).has("new")) {
  history.replaceState(null, "", "./mypage.html");
  void openNewChat();
}
document.addEventListener("onecut-authenticated", renderChats);
void renderChats();
