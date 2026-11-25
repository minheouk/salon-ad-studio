/** 두 모델 실험 화면의 파일 읽기와 첨부 미리보기를 제공합니다. */
globalThis.modelLabFiles = Object.freeze({
  // 파일을 요청 또는 실험 기록에 포함할 수 있는 데이터 URL로 읽습니다.
  readDataUrl(blob) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.addEventListener("load", () => resolve(reader.result));
      reader.addEventListener("error", () => reject(reader.error));
      reader.readAsDataURL(blob);
    });
  },

  // 화면별 제거 동작을 유지하면서 같은 첨부 미리보기를 구성합니다.
  renderAttachments(container, entries, removeAttachment) {
    container.replaceChildren(...entries.map((entry, index) => {
      const thumbnail = document.createElement("div");
      thumbnail.className = "attachment";
      const image = new Image();
      image.src = entry.url;
      image.alt = entry.file.name;
      thumbnail.append(image);
      if (removeAttachment) {
        const remove = document.createElement("button");
        remove.type = "button";
        remove.textContent = "×";
        remove.setAttribute("aria-label", `${entry.file.name} 첨부 제거`);
        remove.addEventListener("click", () => removeAttachment(entry, index));
        thumbnail.append(remove);
      }
      return thumbnail;
    }));
  },
});
