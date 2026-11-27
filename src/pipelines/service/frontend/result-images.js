// 서버 결과의 종류와 순서에 따라 공통 이미지 제목과 표시 스타일을 붙입니다.
export function describeResultImages(results) {
  let nailNumber = 0;
  return (results?.results ?? []).map((image) => {
    if (image.type === "price_table") {
      return { ...image, title: "가격표", style: "price-preview" };
    }
    nailNumber += 1;
    return {
      ...image,
      title: `네일팁 이미지 ${nailNumber}`,
      style: nailNumber % 2 ? "nail-preview" : "nail-preview alternate",
    };
  });
}
