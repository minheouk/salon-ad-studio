/** 모델 서버 연결과 목업 응답을 한 곳에서 관리합니다. */
const modelEndpoints = {
  image: '',
  text: '',
};

async function requestGeneration(mode, input, images) {
  const endpoint = modelEndpoints[mode];
  if (!endpoint) {
    if (mode === 'image') {
      return {images: [
        {url: '../winter-nail-tips.png', label: '네일 팁 · 원본 예시'},
        {url: '../collage-example.png', label: '착용컷 콜라주 · 결과 예시'},
      ]};
    }
    return {
      caption: (input.shop ? input.shop + '의 ' : '') + '이달의 아트를 소개합니다. ❄️\n깊은 버건디와 은은한 라벤더, 반짝이는 실버 포인트로 손끝에 겨울을 담아보세요.',
      hashtags: '#이달의아트 #네일아트 #겨울네일',
    };
  }

  const body = new FormData();
  for (const [key, value] of Object.entries(input)) body.append(key, value);
  for (const image of images) body.append('images', image, image.name);
  const response = await fetch(endpoint, {method: 'POST', body});
  if (!response.ok) throw new Error('생성 요청에 실패했습니다. (HTTP ' + response.status + ')');
  const result = await response.json();
  const imageResult = mode === 'image' && Array.isArray(result?.images) &&
    result.images.length > 0 && result.images.every((item) => item && typeof item.url === 'string' && item.url.trim());
  const textResult = mode === 'text' && typeof result?.caption === 'string' &&
    (result.hashtags === undefined || typeof result.hashtags === 'string');
  if (!imageResult && !textResult) throw new Error('응답 형식이 생성 유형과 맞지 않습니다.');
  return result;
}
