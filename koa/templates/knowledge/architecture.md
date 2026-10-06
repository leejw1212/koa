# <클러스터> 구조 설명

<!-- 자유롭게 쓴다. KOA 는 분석 대상 구성요소 이름이 나오는 단락만 읽는다. -->

## 전체 구조

예: 쇼핑 서비스. ingress-nginx 뒤에 gateway, 그 뒤에 order-api·payment-api. 주문 처리는 RabbitMQ 로 비동기.
DB 는 클러스터 밖 RDS(MySQL).

## HTTP 흐름

예: 사용자 → ingress-nginx(shop.example.com) → gateway → /api/orders → order-api → orders-db

## 비동기 흐름

예: order-api 가 exchange `orders` 로 publish → queue `order.created` → order-worker 가 소비 → orders-db 저장, payment-api 호출

## 운영 관행 · 주의할 점

예: 정기 배포 화·목 14~16시 KST. 매일 02:00 정산 배치 때 큐가 크게 쌓였다 빠진다.
