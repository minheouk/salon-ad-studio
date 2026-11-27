import { deleteAdSession, request } from "./api.js";

// 채팅 ID를 안전하게 인코딩해 상세 API 경로를 만듭니다.
const chatPath = (id) => `/api/ad-sessions/${encodeURIComponent(id)}`;

// 채팅 목록과 서버의 보관 정책을 함께 조회합니다.
export function listChats() {
  return request("/api/ad-sessions?include=policy", "GET");
}

// 채팅 상세 정보를 읽고 저장된 첨부 사진이 있으면 복원합니다.
export async function loadChat(id) {
  const chat = await request(chatPath(id), "GET");
  if (chat.photoName) {
    const response = await fetch(`${chatPath(id)}/photo`);
    if (!response.ok) throw new Error("첨부 사진을 불러오지 못했습니다.");
    chat.photo = new File([await response.blob()], chat.photoName, { type: chat.photoType });
  }
  return chat;
}

// 초안을 저장하며 기존 사진과 달라졌을 때만 파일을 첨부합니다.
export function saveChat(chat, savedPhoto) {
  const { id, photo, ...draft } = chat;
  const keepPhoto = photo === savedPhoto;
  const body = new FormData();
  body.append("draft", JSON.stringify({ ...draft, keepPhoto }));
  if (photo && !keepPhoto) body.append("photo", photo);
  return request(chatPath(id), "PUT", body);
}

export const deleteChat = deleteAdSession;

// 서버가 보낸 시각을 한국어 날짜와 시간으로 표시합니다.
export function dateLabel(timestamp) {
  return new Intl.DateTimeFormat("ko-KR", {
    month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false,
  }).format(timestamp);
}
