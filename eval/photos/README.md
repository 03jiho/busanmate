# 사진 테스트셋 (AC-005, AI-REQ-06)

색인(place_image)에 **쓰지 않은** 사진으로 사진 인식 Top-5 정확도(목표 70%)를 측정한다.

## 구조

```
eval/photos/
├── labels.csv        정답표
├── 0001.jpg          사진 (JPG/PNG, 10MB 이하)
└── ...
```

| labels.csv 열 | 내용 |
|---|---|
| file | 이 폴더 안의 파일 이름 |
| place_id | 정답 장소의 DB `place.place_id` |
| place_name | 사람이 확인하기 위한 장소 이름 |
| taken_by | 촬영자(팀원) 또는 출처 |
| license | 이용 조건 (직접 촬영이면 "직접 촬영") |

## 규칙

- 직접 촬영했거나 이용 조건이 확인된 사진만 쓴다(NFR-007). 색인에 들어간 공공 API 대표 이미지는 쓰지 않는다.
- 규모는 계획서 초안대로 명소 약 20곳 × 3장. 라벨은 다른 팀원 1명이 교차 확인한다.
- 사람 얼굴이 크게 나오지 않게 한다(개인정보).

## 실행

`python -m eval.photo_eval eval/photos` → Top-1, Top-5, 인식률(최고 유사도 ≥ τ, `config.yaml`의 `image.match_threshold`).
