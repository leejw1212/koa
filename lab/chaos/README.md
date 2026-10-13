# lab/chaos — kind-lab 장애 주입 + KOA vs 일반 프로필 비교

kind-lab(로컬 kind) 전용. 관리자 컨텍스트로 장애를 넣고, 두 Hermes 프로필에 **같은 증상 질문**을 동시에 던져
분석 결과를 나란히 비교한다. 분석하는 쪽은 둘 다 읽기 전용 SA(`~/.kube/hermes-readonly.yaml`)만 쓴다.

| 파일 | 하는 일 |
|---|---|
| `loadgen.yaml` | `traffic/loadgen` — 6초마다 인그레스 → 링크카드 "카드 만들기" (대상은 클러스터 안 slowsite). 요청이 있어야 장애 증거가 로그에 쌓인다 |
| `chaos.py` | 장애 주입기. `inject`(무작위/지정) · `status`(정답 숨김) · `reveal` · `recover`. 정답은 `~/.koa-chaos/answer.json`(base64, 저장소 밖) |
| `compare.py` | 지금 걸린 장애의 질문을 `koa`·`plain` 에 동시에 `hermes -p <p> chat -q` 로 던지고 `~/.koa-chaos/runs/<시각>/summary.md` 에 비교표. `--repeat N` 이면 같은 질문을 N 번씩 돌려 프로필별 중앙값(맞힘 수·시간·도구 호출·탐색·토큰)도 낸다 |

## 비교 조건 (공정성)

| | koa | plain |
|---|---|---|
| 모델·추론 | claude-opus-5-5, reasoning medium | 같음 |
| 클러스터 권한 | 읽기 전용 SA (터미널 kubectl + kubernetes MCP) | 같음 |
| 그 밖의 MCP | opensearch · prometheus · grafana · argocd | 없음 |
| SOUL · 스킬 | KOA SOUL, cluster-incident-analysis 등 | 기본 Hermes SOUL, 기본 스킬 |
| 도구·지식 | `koa/knowledge.py context`, 클러스터 지식(흐름·정상 패턴·과거 이슈) | 없음 |

`plain` 프로필은 기본 프로필을 복제(`hermes profile create plain --clone-from default`)한 뒤 MCP 를 kubernetes 하나로 줄이고,
`skills.external_dirs`(KOA 스킬)를 빼고, 터미널 `KUBECONFIG` 를 읽기 전용으로 고정했다. 메모리에는 "kind-lab, 읽기 전용, 변경 금지" 한 줄만.
비교하는 것: **같은 모델에 KOA 의 도구·지식·절차를 얹었을 때 무엇이 달라지나**.

## 한 판 돌리기

```bash
K="--kubeconfig ~/.kube/config --context kind-lab"
kubectl $K apply -f lab/chaos/loadgen.yaml          # 한 번만
python3 lab/chaos/chaos.py inject                    # 무작위 장애 (--scenario 로 지정, --delay-max 10 이면 0~10분 뒤)
sleep 180                                            # 증거가 쌓이게
python3 lab/chaos/compare.py                         # 두 프로필 동시 분석 → summary.md
python3 lab/chaos/chaos.py recover
```

개선 전후를 견줄 때는 한 판으로 판단하지 않는다. 같은 모델도 판마다 도는 길이 달라서, 한 판의 시간·토큰 차이는 운일 수 있다.

```bash
python3 lab/chaos/compare.py --repeat 3 --profiles koa   # 같은 장애를 세 번 → 프로필별 중앙값
python3 koa/stats.py --profile koa --days 1             # 같은 실행을 Hermes 세션 기록으로 (질문별 도구 호출 순서는 --session <id>)
python3 koa/stats.py --profile plain --all --days 1     # plain 은 knowledge.py 를 안 부르므로 --all
```

- 회차는 차례로 돌고, 한 회차 안에서 프로필끼리는 동시에 돈다. 에이전트 cwd 는 실행마다 빈 폴더(`cwd/<실행>/`)라 앞 회차의 답이 보이지 않는다.
- 회차끼리 완전히 독립은 아니다: KOA 는 분석하다 배운 것을 `koa-learned` 스킬에 바로 적으므로 뒤 회차가 앞 회차의 교훈을 읽을 수 있다.
  교훈 효과를 빼고 보려면 회차 사이에 `koa-learned` 를 되돌리거나, 한 판씩 날을 나눠 돌린다.
- `탐색` 은 OpenSearch 인덱스 목록·매핑을 다시 더듬은 호출 수(`ListIndexTool`·`IndexMappingTool`)다. 지식에 보는 법이 있으면 줄어야 한다.

## 시나리오

| 이름 | 증상(질문) | 원인 | 왜 골랐나 |
|---|---|---|---|
| `callback-blocked` | "만들기를 누르면 접수는 되는데 결과를 기다리는 중에서 안 끝나요" | NetworkPolicy 가 linkcard-web 인입을 ingress-nginx 로만 제한 → 워커의 `/internal/result` 콜백 타임아웃 | 파드·이벤트 전부 정상. 요청 흐름(web→큐→worker→web 콜백)을 알아야 찾는다 |

시나리오 추가: `chaos.py` 의 `SCENARIOS` 에 `symptom/answer/keywords/inject/recover/check` 를 넣는다. 객체 이름에 답이 드러나는
단어(chaos, broken…)는 쓰지 않는다.

## 주의

- GitOps(Argo CD selfHeal) 대상 리소스를 직접 고치면 Argo 가 되돌린다. 시나리오는 Argo 가 관리하지 않는 새 객체를 더하거나,
  되돌려지는 것 자체를 장면으로 쓴다.
- 채점은 키워드 기준이라 거칠다. `summary.md` 의 두 답을 사람이 읽고 판정한다.
