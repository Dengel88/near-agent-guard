# HANDOFF — Agent Guard (второй проект на NEAR HACKATHON)

_Обновлено: 2026-11-12_

## Что это
Платёжный файрвол между ИИ-агентами и деньгами. Трек: AI Security & Defense (+ Agent Economy).
Агент не держит ключей, он только просит платёж через API. Guard: правила → ИИ-скринер (Gemini) → allow / hold / block →
выплата из казны (USDC на NEAR напрямую, другие сети через 1Click) → журнал с хэш-цепочкой.
Первый проект: AI-Штат Pay (github.com/Dengel88/nearhackaton). README обоих должны ссылаться друг на друга (в этом уже есть).

## Состояние
- Код: FastAPI, `app/` (policy, screen, guard, near, store, agent, main), страницы `web/index.html` (демо), `web/console.html` (владелец).
- 18 тестов зелёные: `pytest -q`. Mock-режим работает без ключей.
- Проверено вживую из облака: Gemini gemini-3.8-flash (агент + скринер), 1Click live-котировки USDC NEAR → Base/Solana.
- NEAR-подпись своя (pynacl + borsh), байт-в-байт совпадает с py-near-primitives. Живую отправку не проверяли: из облака нет доступа к NEAR RPC и нет ключа казны.

## Прод (12.11)
- https://near-agent-guard.vercel.app — LIVE: Gemini, 1Click live, EXECUTION_MODE=live, Turso near-agent-guard-dengel88.
- Казна implicit 575208d0…639fe999. Ключ и OWNER_TOKEN только в Vercel и у Сэма (D:\AI\NEAR\*.txt).
- Посетители: симулированная песочница; реальные деньги — только владелец (/console → Owner sign-in).
- Реальные tx: $1 → wanderlust.near 8wvphK68ZurtEKgfxDa3xqW1RQ8VYDKrHZWhmJgbphJK; $2 → Base через 1Click, депозит DMjVG3zZew7JBjzvP9SwP1QSywtorQERLeSJsBBSbnQg (1Click долго PROCESSING, дедлайн 02:41 UTC, иначе refund).
- Видео: D:\AI\NEAR\agent_guard_live_cut_v2.mp4 (2:19, без звука), исходники в D:\AI\NEAR\recordings\.

## Дальше
- [ ] Кадр «paid» для Base (после SUCCESS) или честно оставить settling.
- [ ] Озвучка/титры к видео, пост в X (@NEARProtocol #NEARHackathon), форма до 14.11 23:59 UTC.
- [ ] В README AI-Штат Pay добавить ссылку на этот проект (правило про 2 проекта).
- [ ] Идея-усиление: жёсткие лимиты on-chain.
