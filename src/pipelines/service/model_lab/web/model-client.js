/** 모델 실험의 프롬프트와 이미지 파일을 서비스 백엔드로 전달합니다. */

// 브라우저의 이미지 파일을 JSON 요청에 담을 수 있는 형태로 변환합니다.
function imageDataUrl(image) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.addEventListener("load", () => resolve(reader.result));
    reader.addEventListener("error", () => reject(new Error("첨부 이미지를 읽지 못했습니다.")));
    reader.readAsDataURL(image);
  });
}

// 서비스 백엔드에서 생성한 PNG를 받아 화면에 전달합니다.
export async function generateAd({ prompt, additionalImages, resultImage, imageModel }) {
  const response = await fetch("/api/model-lab/generate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      prompt,
      imageModel,
      additionalImages: await Promise.all(additionalImages.map(imageDataUrl)),
      resultImage: resultImage ? await imageDataUrl(resultImage) : null,
    }),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.message || "광고를 만들지 못했습니다. 다시 시도해 주세요.");
  }
  return response.blob();
}
