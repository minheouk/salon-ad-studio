import "./app.js";
import { editAdImages, generateAd, retryDelay } from "./api.js";
import { listChats, loadChat, saveChat } from "./chats.js";
import { getPublishDraft, restorePublishDraft, setApiResults } from "./publish.js";
import { describeResultImages } from "./result-images.js";

const MAX_PHOTO_BYTES = 10 * 1024 * 1024;
const MAX_PROMPT_LENGTH = 200;
const PHOTO_TYPES = ["image/jpeg", "image/png"];

const form = document.querySelector("#create-form");
const photoInput = document.querySelector("#photo");
const photoDrop = document.querySelector("#photo-drop");
const photoPreview = document.querySelector("#photo-preview");
const photoImage = document.querySelector("#photo-image");
const photoName = document.querySelector("#photo-name");
const photoError = document.querySelector("#photo-error");
const prompt = document.querySelector("#prompt");
const promptCount = document.querySelector("#prompt-count");

let selectedPhoto = null;
let savedPhoto = null;
let previewUrl = "";

// 첨부 파일의 개수와 형식, 크기를 검사해 오류 문구를 반환합니다.
function photoValidationMessage(files) {
  if (files.length !== 1) return "사진을 한 장만 첨부해 주세요.";
  const [file] = files;
  if (!PHOTO_TYPES.includes(file.type)) return "JPG 또는 PNG 사진을 선택해 주세요.";
  if (file.size > MAX_PHOTO_BYTES) return "10 MiB 이하의 사진을 선택해 주세요.";
  return "";
}

// 첨부 오류 문구와 입력창의 접근성 상태를 갱신합니다.
function showPhotoError(message) {
  photoError.textContent = message;
  photoInput.setAttribute("aria-invalid", String(Boolean(message)));
}

// 첨부 사진과 미리보기 주소를 정리하고 화면에서 숨깁니다.
function clearPhoto() {
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  previewUrl = "";
  selectedPhoto = null;
  photoImage.removeAttribute("src");
  photoName.textContent = "";
  photoPreview.hidden = true;
}

// 검증한 사진의 미리보기를 표시하고 새 첨부만 저장하도록 예약합니다.
function selectPhoto(files, { restored = false } = {}) {
  const message = photoValidationMessage(files);
  showPhotoError(message);
  if (message) return;

  clearPhoto();
  [selectedPhoto] = files;
  previewUrl = URL.createObjectURL(selectedPhoto);
  photoImage.src = previewUrl;
  photoName.textContent = selectedPhoto.name;
  photoPreview.hidden = false;
  if (restored) savedPhoto = selectedPhoto;
  else scheduleSave();
}

photoInput.addEventListener("change", () => {
  if (photoInput.files.length) selectPhoto(photoInput.files);
  photoInput.value = "";
});

photoDrop.addEventListener("dragover", (event) => {
  event.preventDefault();
  photoDrop.classList.add("is-dragging");
});
photoDrop.addEventListener("dragleave", (event) => {
  if (!photoDrop.contains(event.relatedTarget)) photoDrop.classList.remove("is-dragging");
});
photoDrop.addEventListener("drop", (event) => {
  event.preventDefault();
  photoDrop.classList.remove("is-dragging");
  selectPhoto(event.dataTransfer.files);
});

document.querySelector("#remove-photo").addEventListener("click", () => {
  clearPhoto();
  showPhotoError("");
  photoInput.focus();
  scheduleSave();
});

photoImage.addEventListener("error", () => {
  if (!previewUrl) return;
  clearPhoto();
  showPhotoError("사진을 열 수 없습니다. 다른 사진을 선택해 주세요.");
});

// 프롬프트 글자 수를 표시하고 입력 제한을 적용합니다.
function validatePrompt(field, counter) {
  const length = Array.from(field.value.trim()).length;
  counter.textContent = `${length} / ${MAX_PROMPT_LENGTH}자`;
  counter.classList.toggle("field-error", length > MAX_PROMPT_LENGTH);
  if (!length && field.required) field.setCustomValidity("프롬프트를 입력해 주세요.");
  else if (length > MAX_PROMPT_LENGTH) field.setCustomValidity("프롬프트는 200자까지 입력할 수 있습니다.");
  else field.setCustomValidity("");
}

// 매장 정보 입력값을 확인하고 각 필드의 오류 상태를 갱신합니다.
function validateShopFields() {
  for (const name of ["shop", "instagramId", "businessHours", "booking"]) {
    const field = form.elements.namedItem(name);
    field.value = field.value.trim();
    field.setCustomValidity("");
  }

  const account = form.elements.instagramId;
  if (account.value.startsWith("@")) account.setCustomValidity("@를 빼고 계정명을 입력해 주세요.");

  const booking = form.elements.booking;
  if (booking.value && !/^https?:\/\//i.test(booking.value)) {
    booking.setCustomValidity("http:// 또는 https://로 시작하는 예약 링크를 입력해 주세요.");
  }
}

form.addEventListener("input", (event) => {
  if (event.target === prompt) validatePrompt(prompt, promptCount);
  else event.target.setCustomValidity("");
  scheduleSave();
});

form.addEventListener("submit", (event) => {
  event.preventDefault();
  showPhotoError(selectedPhoto ? "" : "네일 사진을 첨부해 주세요.");
  if (!selectedPhoto) {
    photoInput.focus();
    return;
  }
  validateShopFields();
  validatePrompt(prompt, promptCount);
  if (form.reportValidity()) generateExample();
});

const createTitle = document.querySelector("#create-title");
const resultPanel = document.querySelector("#example-results");
const resultTitle = document.querySelector("#result-title");
const commonPrompt = document.querySelector("#common-edit-prompt");
const commonPromptCount = document.querySelector("#common-edit-count");
const editDialog = document.querySelector("#image-edit-dialog");
const editForm = document.querySelector("#image-edit-form");
const imagePrompt = document.querySelector("#image-edit-prompt");
const imagePromptCount = document.querySelector("#image-edit-count");
const selectedImagePreview = document.querySelector("#selected-image-preview");
const selectedImageName = document.querySelector("#selected-image-name");
let activeImageIndex;
let imagePrompts = [];
let apiResults;
const resultGrid = document.querySelector(".result-grid");
const resultCount = document.querySelector("#result-count");

// 이미지 카드에 저장된 개별 수정 지시를 표시합니다.
function renderPrompt(card, index) {
  const draft = card.querySelector(".image-edit-draft");
  draft.textContent = imagePrompts[index] ?? "";
  draft.hidden = !draft.textContent;
}

// 서버가 반환한 이미지와 해당 이미지의 수정 버튼을 카드로 만듭니다.
function createResultCard(image, index) {
  const card = document.createElement("figure");
  const visual = document.createElement("div");
  visual.className = `image-placeholder ${image.style}`;
  const picture = document.createElement("img");
  picture.src = image.image_url;
  picture.alt = image.title;
  visual.append(picture);
  const label = document.createElement("figcaption");
  label.textContent = image.title;
  card.append(visual, label);
  if (image.type === "nail_tip") {
    const editor = document.createElement("div");
    editor.className = "result-image-edit";
    editor.innerHTML = '<p class="image-edit-draft" hidden></p><button class="button button-outline" type="button">이 이미지 수정</button>';
    editor.querySelector("button").dataset.imageEdit = index;
    card.append(editor);
    renderPrompt(card, index);
  }
  return card;
}

// 결과 카드와 내보내기 목록을 갱신하고 결과 화면을 엽니다.
function showResults() {
  const images = describeResultImages(apiResults);
  setApiResults(apiResults, images);
  resultGrid.replaceChildren(...images.map(createResultCard));
  resultCount.textContent = `생성 결과 ${images.length}장`;
  createTitle.hidden = true;
  form.hidden = true;
  resultPanel.hidden = false;
  resultTitle.focus();
}

document.querySelector("#back-to-inputs").addEventListener("click", () => {
  resultPanel.hidden = true;
  createTitle.hidden = false;
  form.hidden = false;
  photoInput.focus();
});

commonPrompt.addEventListener("input", () => {
  validatePrompt(commonPrompt, commonPromptCount);
  scheduleSave();
});
imagePrompt.addEventListener("input", () => validatePrompt(imagePrompt, imagePromptCount));

resultGrid.addEventListener("click", (event) => {
  const button = event.target.closest("[data-image-edit]");
  if (!button) return;
  const card = button.closest("figure");
  const visual = card.querySelector(".image-placeholder");
  activeImageIndex = Number(button.dataset.imageEdit);
  selectedImageName.textContent = card.querySelector("figcaption").textContent;
  selectedImagePreview.replaceChildren(visual.querySelector("img").cloneNode());
  selectedImagePreview.classList.toggle("alternate", visual.classList.contains("alternate"));
  imagePrompt.value = imagePrompts[activeImageIndex] ?? "";
  editStatus.textContent = "";
  validatePrompt(imagePrompt, imagePromptCount);
  editDialog.showModal();
  imagePrompt.focus();
});

document.querySelector("#cancel-image-edit").addEventListener("click", () => editDialog.close());

const editStatus = document.querySelector("#image-edit-status");
const batchStatus = document.querySelector("#batch-edit-status");
const applyEdits = document.querySelector("#apply-edits");
const retryEdits = document.querySelector("#retry-edits");
let failedEdits = [];
let editing = false;
let editCooldownUntil = 0;

// 개별 수정 지시를 검증해 초안에 반영하고 저장을 예약합니다.
function saveImagePrompt() {
  if (!imagePrompt.reportValidity()) return false;
  imagePrompts[activeImageIndex] = imagePrompt.value.trim();
  renderPrompt(resultGrid.children[activeImageIndex], activeImageIndex);
  scheduleSave();
  return true;
}

document.querySelector("#save-image-prompt").addEventListener("click", () => {
  if (saveImagePrompt()) editDialog.close();
});

// 공통 지시나 개별 지시가 적용되는 네일팁 이미지를 요청 대상으로 모읍니다.
function editTargets() {
  const common = commonPrompt.value.trim();
  return (apiResults?.results ?? []).flatMap((image, index) => {
    const image_prompt = imagePrompts[index] ?? "";
    if (image.type !== "nail_tip" || (!common && !image_prompt)) return [];
    return [{ index, image_id: image.image_id, image_prompt }];
  });
}

// 이미지 수정 진행 상태에 맞춰 조작 버튼과 입력창을 갱신합니다.
function showEditProgress(busy) {
  editing = busy;
  resultPanel.querySelectorAll("button").forEach((button) => { button.disabled = busy; });
  editForm.querySelectorAll("button").forEach((button) => { button.disabled = busy; });
  commonPrompt.readOnly = busy;
  imagePrompt.readOnly = busy;
}

// 이미지 수정을 요청하고 성공한 결과와 재시도 대상을 각각 반영합니다.
async function submitImageEdits(targets, common, status) {
  if (editing || Date.now() < editCooldownUntil) return false;
  if (!targets.length) {
    status.textContent = "공통 또는 이미지별 수정 프롬프트를 입력해 주세요.";
    return false;
  }
  if (!apiResults || targets.some((target) => !target.image_id)) {
    status.textContent = "입력 화면에서 이미지를 다시 생성한 뒤 수정해 주세요.";
    return false;
  }

  failedEdits = [];
  retryEdits.hidden = true;
  status.textContent = `이미지 ${targets.length}장을 수정하고 있습니다…`;
  showEditProgress(true);
  try {
    const images = targets.map(({ image_id, image_prompt }) => ({ image_id, image_prompt }));
    const response = await editAdImages(apiResults.session_id, common, images);
    const byId = new Map(targets.map((target) => [target.image_id, target]));
    for (const item of response.results) {
      const target = byId.get(item.image_id);
      if (item.image) apiResults.results[target.index] = item.image;
      else failedEdits.push({ target, common, message: item.error.message });
    }
    const delay = Math.max(0, ...response.results.map((item) => retryDelay(item.error?.retry_after)));
    if (delay) {
      editCooldownUntil = Date.now() + delay;
      setTimeout(() => showEditProgress(false), delay);
    }
    const completed = targets.length - failedEdits.length;
    const failure = failedEdits.length ? ` 실패 ${failedEdits.length}장. ${failedEdits[0].message}` : "";
    batchStatus.textContent = `수정 완료 ${completed}장.${failure}`;
    retryEdits.hidden = !failedEdits.length;
    showResults();
    storeDraft();
    return true;
  } catch (error) {
    status.textContent = error.message;
    if (status === batchStatus && (!error.status || [429, 502, 504].includes(error.status))) {
      failedEdits = targets.map((target) => ({ target, common }));
      retryEdits.hidden = false;
    }
    if (error.retryDelay) {
      editCooldownUntil = Date.now() + error.retryDelay;
      setTimeout(() => showEditProgress(false), error.retryDelay);
    }
    return false;
  } finally {
    if (Date.now() >= editCooldownUntil) showEditProgress(false);
  }
}

applyEdits.addEventListener("click", () => {
  if (commonPrompt.reportValidity()) void submitImageEdits(editTargets(), commonPrompt.value.trim(), batchStatus);
});

retryEdits.addEventListener("click", () => {
  const targets = failedEdits.map(({ target }) => target);
  void submitImageEdits(targets, failedEdits[0]?.common ?? "", batchStatus);
});

editForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!imagePrompt.value.trim()) {
    editStatus.textContent = "수정 프롬프트를 입력해 주세요.";
    return;
  }
  if (!saveImagePrompt()) return;
  const index = activeImageIndex;
  const target = {
    index,
    image_id: apiResults?.results[index]?.image_id,
    image_prompt: imagePrompts[index],
  };
  if (await submitImageEdits([target], "", editStatus)) editDialog.close();
});

editDialog.addEventListener("cancel", (event) => {
  if (editing) event.preventDefault();
});
editDialog.querySelector(".auth-close").addEventListener("click", (event) => {
  if (editing) event.preventDefault();
});

const generationExample = document.querySelector("#generation-example");
const generationNotice = document.querySelector("#generation-notice");
const generationAction = generationNotice.querySelector("button");
const storageNotice = document.querySelector("#storage-notice");
const fieldNames = ["shop", "instagramId", "businessHours", "booking", "prompt"];
const errors = {
  failed: ["이미지를 생성하지 못했습니다. 첨부 이미지와 입력값은 유지됩니다.", "다시 시도"],
  size: ["요청 크기가 너무 큽니다. 더 작은 사진으로 바꿔 주세요.", "사진 교체"],
  format: ["요청을 전송하지 못했습니다. 입력은 유지됩니다. 새로고침한 뒤 다시 시도해 주세요.", ""],
};
let retryTimer;
let generationRetryUntil = 0;
let saveTimer;
let pendingSave = Promise.resolve();
let chatId = new URLSearchParams(location.search).get("chat");
let hasResults = false;

// 현재 입력으로 이미지 생성을 요청하고 결과나 재시도 안내를 표시합니다.
async function generateExample() {
  if (Date.now() < generationRetryUntil) return;
  clearTimeout(retryTimer);
  generationNotice.hidden = true;
  const state = generationExample.value;
  if (errors[state]) {
    const [message, action] = errors[state];
    generationNotice.querySelector("p").textContent = message;
    generationAction.textContent = action;
    generationAction.hidden = !action;
    generationAction.disabled = false;
    generationNotice.hidden = false;
    return;
  }
  const button = form.querySelector('button[type="submit"]');
  if (button.disabled) return;
  const body = new FormData(form);
  chatId ??= crypto.randomUUID();
  body.append("session_id", chatId);
  const photo = selectedPhoto;
  body.append("images", photo);
  button.disabled = true;
  button.textContent = "생성 중…";
  try {
    const generation = pendingSave.then(async () => {
      const result = await generateAd(body, state);
      savedPhoto = photo;
      return result;
    });
    pendingSave = generation.catch(() => {});
    apiResults = await generation;
    imagePrompts = apiResults.results.map((_, index) => imagePrompts[index] ?? "");
    failedEdits = [];
    retryEdits.hidden = true;
    batchStatus.textContent = "";
    hasResults = true;
    showResults();
    storeDraft();
  } catch (error) {
    generationNotice.querySelector("p").textContent = error.message;
    generationAction.textContent = "다시 시도";
    generationAction.hidden = false;
    generationAction.disabled = Boolean(error.retryDelay);
    generationNotice.hidden = false;
    if (error.retryDelay) {
      generationRetryUntil = Date.now() + error.retryDelay;
      generationAction.textContent = `다시 시도 (${Math.ceil(error.retryDelay / 1000)}초 후)`;
      retryTimer = setTimeout(() => {
        generationAction.disabled = false;
        generationAction.textContent = "다시 시도";
        button.disabled = false;
      }, error.retryDelay);
    }
  } finally {
    button.disabled = Date.now() < generationRetryUntil;
    button.textContent = "생성하기";
  }
}

generationAction.addEventListener("click", () => {
  if (generationExample.value === "size") {
    photoInput.click();
    return;
  }
  generationExample.value = "success";
  form.requestSubmit();
});

// 연속 입력이 멈춘 뒤 한 번만 초안을 저장하도록 예약합니다.
function scheduleSave() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(storeDraft, 300);
}

// 현재 초안의 스냅샷을 만들고 앞선 저장이 끝난 뒤 서버에 보냅니다.
function storeDraft() {
  clearTimeout(saveTimer);
  saveTimer = undefined;
  const fields = Object.fromEntries(fieldNames.map((name) => [name, form.elements[name].value]));
  if (!chatId && !selectedPhoto && !Object.values(fields).some((value) => value.trim())) return;

  chatId ??= crypto.randomUUID();
  const chat = {
    id: chatId, fields, photo: selectedPhoto,
    commonPrompt: commonPrompt.value, imagePrompts: (apiResults?.results ?? []).flatMap((image, index) =>
      image.type === "nail_tip" ? [imagePrompts[index] ?? ""] : []),
    publishDraft: getPublishDraft(),
  };
  pendingSave = pendingSave.then(async () => {
    try {
      await saveChat(chat, savedPhoto);
      savedPhoto = chat.photo;
      const page = new URL(location.href);
      if (!page.searchParams.has("chat")) {
        page.searchParams.set("chat", chat.id);
        history.replaceState(null, "", page);
      }
      storageNotice.textContent = "";
    } catch {
      storageNotice.textContent = "채팅을 저장하지 못했습니다.";
    }
  });
  return pendingSave;
}

// 저장된 초안과 사진, 생성 결과를 읽어 작업 화면에 복원합니다.
async function restoreChat() {
  try {
    const { chats, chatLimit } = await listChats();
    if (!chatId && chats.length >= chatLimit) {
      location.replace("./mypage.html?new");
      return;
    }
    if (!chatId) return;
    if (!chats.some((entry) => entry.id === chatId)) {
      storageNotice.textContent = "이 계정에서 해당 채팅을 찾을 수 없습니다.";
      return;
    }
    const chat = await loadChat(chatId);
    for (const name of fieldNames) form.elements[name].value = chat.fields[name] ?? "";
    if (chat.photo) selectPhoto([chat.photo], { restored: true });
    validatePrompt(prompt, promptCount);
    hasResults = chat.hasResults;
    apiResults = chat.apiResults;
    commonPrompt.value = chat.commonPrompt ?? "";
    validatePrompt(commonPrompt, commonPromptCount);
    let promptIndex = 0;
    imagePrompts = (apiResults?.results ?? []).map((image) =>
      image.type === "nail_tip" ? (chat.imagePrompts?.[promptIndex++] ?? "") : "");
    restorePublishDraft(chat.publishDraft ?? { caption: chat.caption });
    if (hasResults) showResults();
  } catch {
    storageNotice.textContent = "저장한 채팅을 불러오지 못했습니다.";
  }
}

document.addEventListener("chat-draft-change", scheduleSave);
document.addEventListener("click", async (event) => {
  const link = event.target.closest("a[href]");
  if (!link || event.defaultPrevented || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  if (saveTimer) storeDraft();
  await pendingSave;
  location.assign(link.href);
});
document.addEventListener("onecut-authenticated", restoreChat);
void restoreChat();
