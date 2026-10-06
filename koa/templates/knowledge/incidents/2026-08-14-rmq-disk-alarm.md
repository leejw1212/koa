---
date: 2026-08-14
symptom: queue-backlog          # queue-backlog | latency | errors | crash | pending | log-pipeline | external-dependency
components: [rabbitmq, order-api]
signature:                      # 다음에 이 흔적이 보이면 이 이슈를 1순위로 확인
  logs: ["disk resource limit alarm set", "blocked"]
cause: "rabbitmq PVC 사용률 90% → 디스크 알람 → 생산자 publish 차단"
found_by: "rabbitmq 파드 로그의 alarm 문구"
---
(예시) 생산자 쪽에서 publish 가 멈춘 것처럼 보였지만 원인은 브로커 디스크였다. PVC 증설로 해결.
