# Revisao de Divida Tecnica - microservices-ocr

Data: 2026-07-01

Escopo: analise estatica read-only do projeto `microservices-ocr`, complementada por execucao do `agy` CLI com modelo `Claude Opus 4.6 (Thinking)`.

## Plano de revisao

1. Mapear arquitetura e fluxo principal: entrada FastAPI, criacao de jobs, fila, processamento OCR, storage local e entrega de PDF.
2. Revisar concorrencia e resiliencia: threads, shutdown, retentativa, idempotencia, recuperacao apos falha e persistencia.
3. Revisar seguranca: upload, validacao de PDF, autorizacao, rate limit, exposicao de downloads e superficie dos engines OCR.
4. Revisar limites operacionais: tamanho de arquivos, batch, timeouts, consumo de memoria/CPU/disco e limpeza de temporarios.
5. Revisar qualidade do pipeline OCR/PDF: fallback, heuristica de qualidade, preservacao visual e PDF/A final.
6. Revisar observabilidade: logging, metricas, health/readiness, diagnostico de erro e indicadores de fila.
7. Revisar testes e deploy: isolamento da suite, cobertura por servico, CI, Dockerfile, compose e readiness operacional.

## Resumo arquitetural

- Aplicacao FastAPI monolitica servida por Uvicorn.
- Endpoints de upload/status/download/batch ficam em `app/routes/api.py`.
- Jobs sao processados por uma fila em memoria com `queue.Queue` e threads daemon em `app/services/job_queue.py`.
- Estado dos jobs fica em um dicionario em memoria dentro de `JobStore`; os arquivos ficam em `/tmp/ocr-recognizer/<job_id>/`.
- OCR primario usa `ocrmypdf`; fallback opcional usa EasyOCR e `pdf2image`.
- O resultado final tenta gerar PDF/A via `ocrmypdf`; em modo degradado copia/gera arquivos locais.
- Frontend esta concentrado em `app/templates/index.html` com bastante JavaScript inline.

## Achados priorizados

### Critico

| ID | Achado | Evidencia | Impacto | Acao recomendada |
| --- | --- | --- | --- | --- |
| DT-01 | Estado dos jobs e fila sao volateis | `JobStore.jobs` e singleton em memoria em `app/services/storage_service.py:43-45`; fila em memoria em `app/services/job_queue.py:16-21` | Restart, deploy ou crash perde status de jobs e fila ativa | Migrar estado para SQLite/Postgres/Redis e recuperar jobs `processing` no startup |
| DT-02 | OCR externo sem timeout | `subprocess.run(...)` sem `timeout` em `app/services/ocrmypdf_service.py:54-63` | Um PDF problematico pode prender worker indefinidamente; com 2 workers a fila pode parar | Adicionar timeout configuravel, tratar `TimeoutExpired` e marcar job como `failed` |
| DT-03 | Upload le arquivo inteiro em RAM antes de validar limite | `payload = await file.read()` antes de validar tamanho em `app/routes/api.py:36-42` | Vetor de DoS por upload/batch grande | Validar `Content-Length`, ler em chunks com limite e impor limite de batch |
| DT-04 | Endpoints operacionais sem autenticacao/autorizacao | Endpoints publicos em `app/routes/api.py:58`, `:68`, `:86`, `:113`, `:121`, `:129` | Qualquer cliente na rede pode enfileirar, limpar fila, consultar status e baixar resultados se souber `job_id` | Introduzir API key/JWT, separar permissoes administrativas e rate limit |

### Alto

| ID | Achado | Evidencia | Impacto | Acao recomendada |
| --- | --- | --- | --- | --- |
| DT-05 | Sem limpeza automatica de arquivos temporarios | `cleanup()` existe mas nao ha caller em `app/services/storage_service.py:87-90`; compose monta `./tmp` em `docker-compose.yml:6-7` | Crescimento continuo de disco no host/container | Criar TTL por status e tarefa periodica de cleanup |
| DT-06 | EasyOCR Reader recriado a cada job | `easyocr.Reader(...)` dentro de `process()` em `app/services/easyocr_service.py:25-26` | Latencia e uso de memoria elevados no fallback | Cachear reader lazy/thread-safe por processo |
| DT-07 | Fallback executa OCR duas vezes por pagina | `readtext(image)` e depois `readtext(str(processed_path))` em `app/services/easyocr_service.py:34-37` | Tempo de fallback aproximadamente dobrado, com primeiro resultado descartado | Rodar OCR uma vez, preferindo imagem pre-processada com fallback real |
| DT-08 | Shutdown nao espera processamento real terminar | `thread.join(timeout=1)` em `app/services/job_queue.py:41-42` | Jobs podem morrer como `processing` e deixar PDFs parciais | Implementar graceful shutdown, log e marcacao de jobs interrompidos |
| DT-09 | Validacao de PDF e superficial | Valida apenas content-type/extensao em `app/routes/api.py:28-44` | Binarios invalidos chegam a parsers externos como Ghostscript/Tesseract | Validar magic bytes `%PDF-` e parsing minimo com `PdfReader` antes de enfileirar |

### Medio

| ID | Achado | Evidencia | Impacto | Acao recomendada |
| --- | --- | --- | --- | --- |
| DT-10 | Fallback pode escolher texto pior por regra `OR` | `_is_better()` em `app/services/ocr_orchestrator.py:89-94` | Fallback com razao melhor mas muito menos texto pode substituir resultado primario | Trocar por score ponderado ou criterio `AND` com tolerancia |
| DT-11 | PDF gerado no fallback nao preserva layout original | `PdfBuilder.build()` cria novo canvas A4 quando `base_pdf_path` e `None` em `app/services/pdf_builder.py:12-47` | Usuario recebe PDF visualmente diferente do original | Criar overlay de texto pesquisavel sobre paginas originais |
| DT-12 | Heuristica de qualidade muito fraca | `COMMON_WORDS` pequeno e `isalpha()` em `app/utils/quality_evaluator.py:7-32` | Falsos positivos/negativos em documentos com numeros, siglas e termos tecnicos | Revisar scoring, incluir alfanumericos e testes com corpus real |
| DT-13 | `JobStore.update()` aceita campos arbitrarios | `setattr(job, key, value)` em `app/services/storage_service.py:79-85` | Typos criam atributos silenciosos e corrompem estado | Validar nomes de campos e tipos antes de mutar |
| DT-14 | Frontend vulneravel a XSS por interpolacao em `innerHTML` | `item.file.name` e `relativePath` entram em HTML em `app/templates/index.html:383-405` e `:500-531` | Nome de arquivo malicioso pode executar script no painel | Usar `textContent`/DOM APIs ou escape centralizado |
| DT-15 | Docker sem healthcheck/limites/restart | `Dockerfile:56`; `docker-compose.yml:1-7` | Orquestrador nao detecta falha funcional; OCR pode consumir recursos sem contencao | Adicionar `/health`, `HEALTHCHECK`, `restart` e limites de CPU/memoria |

### Baixo

| ID | Achado | Evidencia | Impacto | Acao recomendada |
| --- | --- | --- | --- | --- |
| DT-16 | Sem logging estruturado | Ausencia de uso de `logging`; excecao capturada sem log em `app/services/ocr_orchestrator.py:79-81` | Baixa capacidade de diagnostico em producao | Log JSON com `job_id`, evento, status, duracao e erro |
| DT-17 | Sem metricas/readiness | Ausencia de endpoints `/health`, `/readiness` e `/metrics` | Operacao reativa, sem sinal de fila, erro ou disco | Expor health/readiness e metricas Prometheus |
| DT-18 | Testes insuficientes e pouco isolados | `TestClient(app)` global em `tests/test_api.py:12`; singletons globais nos servicos | Risco de flakiness e regressao invisivel | Fixtures isoladas, mocks de OCR e unitarios por servico |
| DT-19 | Uvicorn single-process | `CMD ["uvicorn", ...]` em `Dockerfile:56` | Escalabilidade limitada; multi-worker exige persistencia antes | Resolver persistencia primeiro, depois avaliar Gunicorn/Uvicorn workers |

## Sprint sugerida de correcao e adequacao

Horizonte: 2 semanas. Prioridade: reduzir risco operacional antes de melhorar UX/refinamentos.

### Epico 1 - Resiliencia e persistencia

- P0: Persistir jobs em SQLite com repositorio explicito.
  - Aceite: jobs criados continuam consultaveis apos restart; testes cobrem create/get/update/list.
- P0: Recuperar jobs `processing` no startup.
  - Aceite: job interrompido volta para `queued` ou `failed` com motivo rastreavel.
- P0: Timeout configuravel para `ocrmypdf`.
  - Aceite: `TimeoutExpired` vira job `failed` com erro claro e worker continua vivo.
- P1: Cleanup por TTL dos diretorios temporarios.
  - Aceite: jobs concluidos/falhos acima do TTL tem arquivos removidos sem quebrar status.

### Epico 2 - Seguranca e limites

- P0: Upload seguro por streaming/chunks e limite de batch.
  - Aceite: arquivos acima do limite sao rejeitados sem carregar tudo em memoria.
- P0: Validar PDF por magic bytes e parsing minimo.
  - Aceite: `.pdf` invalido e rejeitado antes de ser enfileirado.
- P1: API key para endpoints de job e permissao separada para `clear-queue`.
  - Aceite: request sem credencial retorna 401/403.
- P1: Rate limit em upload/status/download.
  - Aceite: burst acima do limite retorna 429.
- P1: Remover XSS no frontend.
  - Aceite: nomes com `<script>` aparecem como texto literal.

### Epico 3 - Qualidade e performance do OCR

- P1: Cachear `easyocr.Reader`.
  - Aceite: reader instanciado uma unica vez por processo em teste com mock.
- P1: Remover OCR duplicado no fallback.
  - Aceite: `readtext` chamado uma vez por pagina em teste unitario.
- P1: Revisar criterio de escolha do fallback.
  - Aceite: testes cobrem casos de menos texto, maior razao, empate e melhoria real.
- P2: Preservar layout original no fallback com overlay pesquisavel.
  - Aceite: PDF final mantem paginas/imagens originais e `pdftotext` extrai texto.

### Epico 4 - Observabilidade, deploy e testes

- P1: Logging estruturado por job.
  - Aceite: logs incluem `job_id`, `event`, `status`, `duration_ms` e erro.
- P1: `/health`, `/readiness` e healthcheck Docker.
  - Aceite: healthcheck falha se disco estiver indisponivel ou app nao responder.
- P1: Docker Compose com `restart`, limites de CPU/memoria e volume documentado.
  - Aceite: compose sobe com limites e caminho de temporarios configuravel.
- P1: Isolar testes e adicionar unitarios dos servicos.
  - Aceite: suite roda sem depender de ordem; OCR externo mockado.
- P2: CI basico com testes e lint.
  - Aceite: pipeline bloqueia PR com teste quebrado.

## Sequenciamento recomendado

1. Timeouts, upload seguro e validacao de PDF.
2. Persistencia de jobs e recovery de startup.
3. Cleanup de temporarios e graceful shutdown.
4. Autenticacao, rate limit e correcao de XSS.
5. Performance/qualidade do fallback OCR.
6. Observabilidade, Docker e CI.

## Validacao executada

- `agy --model "Claude Opus 4.6 (Thinking)" --print ...`: executado com sucesso em modo read-only.
- Relatorio gerado pelo `agy`: `/home/andre/.gemini/antigravity-cli/brain/a6a54dd3-6902-435b-91f5-40adf066c2af/analise_divida_tecnica.md`.
- `pytest -q`: nao disponivel no shell global.
- `.venv/bin/pytest -q`: 9 testes passaram em 1.30s.
