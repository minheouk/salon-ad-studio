const MODEL_CALL_BUDGET_MS = 70_000;
const WEB_RESPONSE_MARGIN_MS = 10_000;

// Retry-After 헤더를 재시도까지 남은 밀리초로 변환합니다.
export function retryDelay(header) {
  const delay = /^\d+$/.test(header ?? "") ? Number(header) * 1000 : Date.parse(header) - Date.now();
  return Number.isFinite(delay) ? Math.max(0, delay) : 0;
}

// 웹 API를 호출하고 JSON 응답이나 상태 정보를 담은 오류를 반환합니다.
export async function request(path, method, payload, example) {
  const json = payload && !(payload instanceof FormData);
  const headers = new Headers();
  if (json) headers.set("Content-Type", "application/json");
  if (example) headers.set("X-Onecut-Example", example);
  const callCount = path === "/api/ads/edit" ? (payload?.images?.length || 1) : 1;
  let response;
  try {
    response = await fetch(path, {
      method, headers, body: json ? JSON.stringify(payload) : payload,
      signal: AbortSignal.timeout(MODEL_CALL_BUDGET_MS * callCount + WEB_RESPONSE_MARGIN_MS),
    });
  } catch (cause) {
    if (cause.name !== "TimeoutError") throw cause;
    throw Object.assign(new Error("요청 시간이 초과됐습니다. 다시 시도해 주세요."), { status: 504 });
  }
  if (response.ok) return response.status === 204 ? null : response.json();

  const data = await response.json().catch(() => null);
  const error = new Error(data?.error?.message ?? "웹 서버에 연결하지 못했습니다.");
  error.status = response.status;
  if (response.status === 401 && !path.startsWith("/api/auth/")) {
    document.dispatchEvent(new Event("onecut-auth-required"));
  }
  error.field = data?.error?.field;
  if (response.status === 429) error.retryDelay = retryDelay(response.headers.get("Retry-After"));
  throw error;
}

// 원본 사진과 매장 정보로 광고 이미지 생성을 요청합니다.
export const generateAd = (fields, example) => request("/api/ads/generate", "POST", fields, example);
// 공통 지시와 이미지별 지시를 묶어 광고 이미지 수정을 요청합니다.
export const editAdImages = (sessionId, commonPrompt, images) =>
  request("/api/ads/edit", "POST", { session_id: sessionId, common_prompt: commonPrompt, images });
// 게시 순서대로 선택한 이미지 ID를 보내 문구와 해시태그를 요청합니다.
export const createCaption = (sessionId, imageIds, example) =>
  request("/api/ads/preview/copy", "POST", { session_id: sessionId, image_ids: imageIds }, example);
// 지정한 채팅을 삭제하도록 백엔드에 요청합니다.
export const deleteAdSession = (sessionId) =>
  request(`/api/ad-sessions/${encodeURIComponent(sessionId)}`, "DELETE");
