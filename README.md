# 아침 뉴스

매일 아침 국내 방송사 9곳(KBS·MBC·SBS·JTBC·YTN·연합뉴스TV·MBN·TV조선·채널A)의 기사를 모아, 같은 사건을 다룬 기사끼리 묶고 섹션별로 많이 보도된 순서로 정리한 페이지를 만듭니다.

- 보는 곳: https://ssiissx.github.io/morning-news/
- 섹션: 오늘의 주요 뉴스 · 날씨 · 정치 · 경제 · 사회 · 국제 · 생활·문화 · 연예 · 스포츠
- 지난 30일치는 `archive/` 에 날짜별로 남습니다.
- GitHub Actions(공개 저장소라 무료)가 실행하므로 PC가 꺼져 있어도, Claude 없이도 돌아갑니다.

## 같은 사건을 어떻게 알아보나

제목 두 개를 두 가지로 비교합니다.

- **뜻**: 한국어 문장 모델 [`jhgan/ko-sroberta-multitask`](https://huggingface.co/jhgan/ko-sroberta-multitask)(약 0.45GB, 무료)이 제목을 숫자 벡터로 바꿔 뜻이 얼마나 가까운지 봅니다. “투표용지 공개검증”과 “투표지 247만 장 검증”처럼 표현이 달라도 같은 사건이면 가깝게 나옵니다.
- **글자**: 두 글자씩 끊은 조각이 얼마나 겹치는지 봅니다.

기준값은 `scripts/build_news.py` 맨 위(`COS_…`, `LEX_…`, `MERGE_COS`)에 있고, 2026-10-28 기사로 맞췄습니다. 모델은 GitHub 서버가 처음 한 번 받아 캐시해 두고, 받지 못한 날에는 글자 비교만으로 묶습니다(페이지 아래에 그렇게 표시돼요). 유료 API나 Claude는 쓰지 않습니다.

## 구성

| 파일 | 하는 일 |
| --- | --- |
| `scripts/feeds.json` | 출처 목록, 섹션, 걸러낼 제목. 여기만 고치면 출처·섹션을 바꿀 수 있어요 |
| `scripts/build_news.py` | 기사를 모아 묶고 `index.html` 을 만드는 프로그램 |
| `scripts/requirements.txt` | 뜻 비교 모델을 돌리는 데 필요한 파이썬 패키지 |
| `.github/workflows/news.yml` | 매일 실행 일정과 배포 |

## 자주 하는 일

**출처 더하기·빼기**: `scripts/feeds.json` 의 `feeds` 배열에 한 줄을 넣거나 지웁니다. GitHub 웹에서 파일을 열고 연필 아이콘으로 고친 뒤 저장하면, 저장하는 순간 다시 만들어집니다.

```json
{"name": "방송사 이름", "section": "politics", "url": "https://.../rss.xml"}
```

- `section`: 그 RSS가 어느 섹션 기사인지(`politics` 정치, `economy` 경제, `society` 사회, `world` 국제, `life` 생활·문화, `ent` 연예, `sports` 스포츠, `weather` 날씨). 섞여 있는 RSS면 빼 두세요. 기사에 붙은 분류(`categories` 표)나 같은 사건을 다룬 다른 방송사 기사를 보고 정합니다.
- `kind: "gnews"`: 공개 RSS가 없는 방송사(KBS·MBC·YTN·채널A)는 구글 뉴스에서 그 사이트만 검색한 결과를 씁니다. 이 기사들은 요약이 없어서, 다른 방송사도 다룬 사건이면 그쪽 요약을 보여 줍니다.
- `exclude`: 걸러낼 제목(정규식). 국회 일정·운세·인사·부고·뉴스 예고 같은 정기 코너가 여기 있어요.
- 섹션의 `limit`: 몇 개까지 보여줄지. 주요 뉴스의 `min_outlets`: 몇 곳 이상 보도한 사건만 올릴지.

**시간 바꾸기**: `news.yml` 의 `cron: "20 1-23/2 * * *"` 는 세계 표준시 홀수 시 20분마다 = 한국 시간 짝수 시 20분마다(오전 6:20, 8:20, … 밤 10:20, 새벽 0:20…), 하루 12번입니다. 한국 시간에서 9시간을 빼서 적으면 됩니다. GitHub 예약은 30분~몇 시간 늦기도 해서 정각을 피했어요.

**지금 바로 만들기**: 저장소의 Actions 탭 → 아침 뉴스 → Run workflow.

**내 PC에서 만들어 보기**:

```
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install -r scripts/requirements.txt
python scripts/build_news.py
```

설치 없이 `python scripts/build_news.py` 만 해도 돌아가고, 그때는 글자 비교로만 묶습니다.

## 멈췄을 때

- Actions 탭에 빨간 표시가 있으면 눌러서 어느 단계가 실패했는지 봅니다. 출처가 5곳 미만으로만 받아지면 일부러 페이지를 바꾸지 않고 실패로 끝납니다(인터넷 문제일 때가 많아요). 다음 날 저절로 다시 돕니다.
- 어떤 방송사가 계속 안 받아지면 페이지 맨 아래 ‘오늘 못 받은 곳’에 줄을 그어 표시합니다. 주소가 바뀐 것이니 `feeds.json` 에서 고치거나 지우세요.
- GitHub는 오래 아무 변화가 없는 저장소의 예약 실행을 멈추는데, 이 저장소는 매일 기록을 커밋하므로 보통 멈추지 않습니다. 혹시 멈추면 Actions 탭에서 ‘Enable workflow’를 누르세요.
