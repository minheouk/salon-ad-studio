import { createCaption } from "./api.js";

let images = [];

const selection = document.querySelector("#export-dialog");
const preview = document.querySelector("#publish-dialog");
const success = document.querySelector("#publish-success");
const failure = document.querySelector("#publish-failure");
const options = document.querySelector("#export-options");
const sequence = document.querySelector("#publish-images");
let scrollbarTimer;

// 포인터가 들어오거나 이미지가 움직이면 스크롤바를 잠시 표시합니다.
function revealImageScrollbar() {
  sequence.classList.add("show-scrollbar");
  clearTimeout(scrollbarTimer);
  scrollbarTimer = setTimeout(() => sequence.classList.remove("show-scrollbar"), 1000);
}

sequence.addEventListener("pointerenter", revealImageScrollbar);
sequence.addEventListener("scroll", revealImageScrollbar, { passive: true });
sequence.addEventListener("wheel", (event) => {
  if (event.ctrlKey || !event.shiftKey || Math.abs(event.deltaX) >= Math.abs(event.deltaY)) return;
  const maxScroll = sequence.scrollWidth - sequence.clientWidth;
  if (maxScroll <= 1 || (event.deltaY < 0 && sequence.scrollLeft <= 1)
      || (event.deltaY > 0 && sequence.scrollLeft >= maxScroll - 1)) return;
  const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? sequence.clientWidth : 1;
  event.preventDefault();
  sequence.scrollLeft += event.deltaY * unit;
}, { passive: false });
const caption = document.querySelector("#publish-caption");
const confirmSelection = document.querySelector("#confirm-export");
const selected = [];
const copyStatus = document.querySelector("#preview-copy-status");
const retryCopy = document.querySelector("#retry-copy");
const publishButton = document.querySelector("#publish");
let captionSource = "";
let copyRequest = 0;
let apiResults;
let copyBusy = false;
let copyRetryUntil = 0;

// 게시글 초안의 변경을 생성 화면의 저장 처리에 알립니다.
function notifyDraftChange() {
  document.dispatchEvent(new Event("chat-draft-change"));
}

// 선택된 이미지의 ID를 게시 순서대로 반환합니다.
function selectedImageIds() {
  return selected.map((index) => images[index].image_id);
}

// 현재 이미지나 순서가 문구 생성 당시와 다르면 재생성을 안내합니다.
function updateCaptionStatus() {
  copyStatus.textContent = caption.value.trim() && captionSource !== JSON.stringify(selectedImageIds())
    ? "현재 이미지와 순서에 맞춰 문구를 다시 생성할 수 있습니다."
    : "";
}

// 문구 생성과 재시도 상태에 따라 편집창과 버튼을 갱신합니다.
function updateCopyControls() {
  retryCopy.disabled = copyBusy || Date.now() < copyRetryUntil;
  caption.readOnly = copyBusy;
  publishButton.disabled = copyBusy || !caption.value.trim();
}

// 선택한 이미지로 게시글 문구를 생성하고 필요한 경우 초안에 저장합니다.
async function generateExampleCaption({ save = true } = {}) {
  if (copyBusy || Date.now() < copyRetryUntil) return;
  const imageIds = selectedImageIds();
  const request = ++copyRequest;
  copyBusy = true;
  copyStatus.textContent = "게시글 문구 생성 중…";
  updateCopyControls();
  try {
    if (!apiResults) throw new Error("입력 화면에서 이미지를 다시 생성해 주세요.");
    const response = apiResults.previewCopy ?? await createCaption(apiResults.session_id, imageIds);
    if (request !== copyRequest) return;
    caption.value = [response.caption, response.hashtags].filter(Boolean).join("\n\n");
    captionSource = JSON.stringify(imageIds);
    updateCaptionStatus();
    if (save) notifyDraftChange();
  } catch (error) {
    if (request === copyRequest) {
      copyStatus.textContent = `${error.message} 다시 생성하거나 직접 입력해 주세요.`;
      if (error.retryDelay) {
        copyRetryUntil = Date.now() + error.retryDelay;
        setTimeout(updateCopyControls, error.retryDelay);
      }
    }
  } finally {
    if (request === copyRequest) {
      copyBusy = false;
      updateCopyControls();
    }
  }
}

// 닫힌 미리보기에서 대기 중인 문구 응답이 반영되지 않게 합니다.
function stopCopyPreview() {
  copyRequest++;
  copyBusy = false;
  updateCopyControls();
}

// 결과 이미지로 내보내기와 미리보기에서 사용할 썸네일을 만듭니다.
function thumbnail(image) {
  const item = document.createElement("span");
  item.className = `image-placeholder ${image.style}`;
  item.setAttribute("aria-hidden", "true");
  if (image.image_url) {
    const picture = document.createElement("img");
    picture.src = image.image_url;
    picture.alt = "";
    item.append(picture);
  } else {
    item.append(document.createElement("i"));
  }
  return item;
}

// 이미지 선택 순번과 선택 장수를 표시하고 확인 버튼을 갱신합니다.
function updateSelection() {
  document.querySelector("#export-count").textContent = `${selected.length}장 선택됨`;
  confirmSelection.disabled = !selected.length || !(apiResults?.session_id || apiResults?.previewCopy);
  for (const button of options.querySelectorAll("button")) {
    const order = selected.indexOf(Number(button.dataset.index));
    button.setAttribute("aria-pressed", String(order !== -1));
    button.querySelector(".selection-order").textContent = order === -1 ? "선택" : `${order + 1}번`;
  }
}

// 내보낼 이미지의 선택 여부를 바꾸고 초안에 반영합니다.
function selectImage(index) {
  const position = selected.indexOf(index);
  if (position === -1) selected.push(index);
  else selected.splice(position, 1);
  updateSelection();
  notifyDraftChange();
}

// 내보내기에서 고를 수 있는 결과 이미지 목록을 표시합니다.
function renderOptions() {
  options.replaceChildren(...images.map((image, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "export-option";
    button.dataset.index = index;
    const name = document.createElement("strong");
    name.textContent = image.title;
    const order = document.createElement("span");
    order.className = "selection-order";
    button.append(thumbnail(image), name, order);
    button.addEventListener("click", () => selectImage(index));
    return button;
  }));
  updateSelection();
}

// 새 결과 목록을 적용하고 유효한 이미지 선택만 유지합니다.
export function setApiResults(results, resultImages) {
  apiResults = results;
  images = resultImages;
  selected.splice(0, selected.length, ...selected.filter((index) => Number.isInteger(index) && images[index]));
  renderOptions();
}

// 선택한 이미지의 게시 순서를 바꾸고 문구 상태와 초안을 갱신합니다.
function moveImage(from, to) {
  if (from < 0 || to < 0 || to >= selected.length || from === to) return;
  const [item] = selected.splice(from, 1);
  selected.splice(to, 0, item);
  renderSequence();
  updateSelection();
  if (!copyBusy) updateCaptionStatus();
  notifyDraftChange();
}

// 이미지의 게시 순서를 한 칸 옮기는 버튼을 만듭니다.
function moveButton(position, step) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "button button-outline";
  button.textContent = step < 0 ? "←" : "→";
  button.setAttribute("aria-label", `${position + 1}번 이미지를 ${step < 0 ? "앞으로" : "뒤로"} 이동`);
  button.disabled = position + step < 0 || position + step >= selected.length;
  button.addEventListener("click", () => {
    moveImage(position, position + step);
    sequence.children[position + step].querySelector("button:not(:disabled)")?.focus();
  });
  return button;
}

// 게시 순서대로 이미지를 표시하고 버튼이나 드래그로 순서를 바꿀 수 있게 합니다.
function renderSequence() {
  sequence.replaceChildren(...selected.map((index, position) => {
    const item = document.createElement("li");
    item.draggable = selected.length > 1;
    const title = document.createElement("span");
    title.textContent = `${position + 1}페이지 · ${images[index].title}`;
    item.append(thumbnail(images[index]), title, moveButton(position, -1), moveButton(position, 1));
    item.addEventListener("dragstart", (event) => event.dataTransfer.setData("text/plain", String(position)));
    item.addEventListener("dragover", (event) => event.preventDefault());
    item.addEventListener("drop", (event) => {
      event.preventDefault();
      const from = event.dataTransfer.getData("text/plain");
      if (/^\d+$/.test(from) && Number(from) < selected.length) moveImage(Number(from), position);
    });
    return item;
  }));
}

// 게시 미리보기를 열고 초안이 비어 있으면 문구만 생성합니다.
function openPreview() {
  renderSequence();
  selection.close();
  preview.showModal();
  sequence.scrollLeft = 0;
  if (!captionSource && !caption.value.trim()) {
    void generateExampleCaption({ save: false });
  } else {
    updateCaptionStatus();
    updateCopyControls();
  }
}

// 현재 게시글 문구와 이미지 선택을 저장용 초안으로 반환합니다.
export function getPublishDraft() {
  return { caption: caption.value, captionSource, selected: [...selected] };
}

// 저장한 게시글 문구와 이미지 선택을 미리보기 화면에 복원합니다.
export function restorePublishDraft(draft) {
  caption.value = draft.caption ?? "";
  captionSource = draft.captionSource ?? "";
  selected.splice(0, selected.length, ...(draft.selected ?? []));
  updateSelection();
  updateCopyControls();
}

caption.addEventListener("input", () => {
  updateCopyControls();
  notifyDraftChange();
});
retryCopy.addEventListener("click", () => {
  void generateExampleCaption();
});
preview.addEventListener("close", () => {
  stopCopyPreview();
  clearTimeout(scrollbarTimer);
  sequence.classList.remove("show-scrollbar");
});
document.querySelector("#open-export").addEventListener("click", () => selection.showModal());
document.querySelector("#cancel-export").addEventListener("click", () => selection.close());
confirmSelection.addEventListener("click", openPreview);
document.querySelector("#back-to-export").addEventListener("click", () => {
  stopCopyPreview();
  preview.close();
  selection.showModal();
});

document.querySelector("#publish").addEventListener("click", () => {
  preview.close();
  if (apiResults?.previewCopy) {
    success.showModal();
    return;
  }
  document.querySelector("#failure-caption").textContent = caption.value;
  document.querySelector("#copy-status").textContent = "";
  document.querySelector("#failure-images").replaceChildren(...selected.map((index) => {
    const item = document.createElement("li");
    item.textContent = images[index].title;
    return item;
  }));
  failure.showModal();
});
document.querySelector("#close-publish-success").addEventListener("click", () => success.close());

document.querySelector("#back-to-preview").addEventListener("click", () => {
  failure.close();
  preview.showModal();
});

document.querySelector("#copy-caption").addEventListener("click", async () => {
  const status = document.querySelector("#copy-status");
  try {
    await navigator.clipboard.writeText(caption.value);
    status.textContent = "게시글 문구를 복사했습니다.";
  } catch {
    status.textContent = "문구를 복사하지 못했습니다. 아래 문구를 직접 선택해 복사해 주세요.";
  }
});

document.querySelector("#save-photos").addEventListener("click", async () => {
  const status = document.querySelector("#copy-status");
  try {
    if (!apiResults) throw new Error("입력 화면에서 이미지를 다시 생성해 주세요.");
    for (const index of selected) {
      const image = apiResults.results[index];
      const response = await fetch(image.image_url);
      if (!response.ok) throw new Error("이미지를 내려받지 못했습니다.");
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.download = response.headers.get("Content-Disposition")?.match(/filename="([^"]+)"/)?.[1]
        ?? `${image.image_id}.png`;
      link.href = url;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    }
  } catch (error) {
    status.textContent = error.message;
  }
});
