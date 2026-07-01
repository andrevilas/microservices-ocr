# OCR Recognizer

MVP da sprint inicial para processamento OCR de PDFs com:

- API FastAPI para upload, status e download
- upload de PDFs de ate 80 MB
- pipeline com OCR primario e fallback opcional por baixa confianca
- retorno final em PDF/A com compactacao
- interface web simples
- armazenamento temporario em `/tmp`
- empacotamento Docker
- CI com GitHub Actions

## Execucao local

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
uvicorn app.main:app --reload
```

## Dependencias OCR opcionais

Para execucao OCR primario real, instale tambem:

```bash
pip install -e .[ocr]
```

Para habilitar o fallback com EasyOCR localmente:

```bash
pip install -e .[ocr_fallback]
```

Build Docker enxuto, apenas com OCR primario:

```bash
docker build -t ocr-recognizer:slim .
docker run -p 8001:8000 ocr-recognizer:slim
```

Build Docker com fallback EasyOCR habilitado:

```bash
docker build --build-arg ENABLE_FALLBACK_OCR=true -t ocr-recognizer:full .
docker run -p 8001:8000 ocr-recognizer:full
```

## Execucao via Docker Compose (Porta 8001)

```bash
docker-compose up --build
```

A aplicacao ficara disponivel em `http://localhost:8001`

O compose inclui `restart: unless-stopped` e limites de recursos (2 CPUs, 2G RAM).
O Dockerfile inclui HEALTHCHECK via python urllib (sem dependencia de curl).

## CI

O pipeline `.github/workflows/ci.yml` roda em push/PR para main:
instala `.[dev]` e executa `pytest -q`.

Sem os engines externos disponiveis, a aplicacao continua executando em modo degradado para desenvolvimento e testes.

## Seguranca

- XSS: nomes de arquivo e caminhos sao escapados via `escapeHtml()` no frontend antes de interpolacao em innerHTML.
- JWT: autenticacao via token JWT (HMAC-SHA256) em cookie HttpOnly `access_token` (browser) ou header `Authorization: Bearer <token>` (API).
- API Key: endpoints `/api/jobs*` aceitam tambem header `X-API-Key` para automacao. API Key **nao** concede acesso de admin.
- Rate Limit: uploads por IP com janela deslizante de 60s (configuravel).
- Senhas: hash PBKDF2-HMAC-SHA256 com salt aleatorio (100k iteracoes). Nenhuma senha em texto claro.

## Autenticacao e Gestao de Usuarios

### Fluxo de autenticacao

1. Acesse `/login` e autentique com email e senha.
2. O servidor retorna um JWT em cookie HttpOnly (`access_token`) e no corpo JSON.
3. Requisicoes subsequentes enviam o cookie automaticamente (browser) ou header `Authorization: Bearer <token>` (API).
4. Logout via `POST /auth/logout` limpa o cookie.

### Admin inicial

O admin e criado automaticamente no startup a partir das variaveis `ADMIN_NAME`, `ADMIN_EMAIL` e `ADMIN_PASSWORD`.

> **IMPORTANTE**: Em producao, troque obrigatoriamente `JWT_SECRET`, `ADMIN_EMAIL` e `ADMIN_PASSWORD` para valores seguros.

Os defaults (`admin@localhost` / `admin`) sao apenas para desenvolvimento.

### Regras de acesso

| Recurso | Quem pode acessar |
|---|---|
| `GET /health`, `GET /readiness` | Publico |
| `GET /login`, `POST /auth/login`, `POST /auth/logout` | Publico |
| `GET /` (pagina principal) | Usuario autenticado (JWT) |
| `GET /metrics` | Usuario autenticado (JWT ou API_KEY) |
| `/api/jobs*` | Usuario autenticado (JWT ou API_KEY) |
| `GET /admin/users` (tela) | Admin (JWT) |
| `GET /api/users`, `POST /api/users` | Admin (JWT). API_KEY nao concede acesso. |

## Configuracao (variaveis de ambiente)

| Variavel | Default | Descricao |
|---|---|---|
| `MAX_UPLOAD_SIZE_MB` | 80 | Limite de tamanho por arquivo PDF |
| `OCR_SUBPROCESS_TIMEOUT_SECONDS` | 300 | Timeout do processo OCR (subprocess) |
| `MAX_BATCH_FILES` | 25 | Maximo de arquivos por upload em lote |
| `API_KEY` | *(vazio)* | Se definido, endpoints `/api/jobs*` aceitam header `X-API-Key` para automacao |
| `UPLOAD_RATE_LIMIT_PER_MINUTE` | 0 | Limite de uploads por IP por minuto (0 = desabilitado) |
| `FALLBACK_CHARACTER_TOLERANCE` | 20 | Tolerancia de caracteres ao comparar fallback vs primario |
| `FALLBACK_MIN_IMPROVEMENT_CHARS` | 20 | Minimo de caracteres extras para preferir fallback |
| `JOB_RETENTION_SECONDS` | 86400 | TTL para limpeza de arquivos de jobs finalizados (completed/failed/canceled) |
| `WORKER_SHUTDOWN_TIMEOUT_SECONDS` | 30 | Timeout para aguardar workers durante shutdown graceful |
| `JWT_SECRET` | `CHANGE-ME-...` | Segredo para assinatura HMAC-SHA256 dos tokens JWT. **Trocar em producao!** |
| `JWT_EXP_MINUTES` | 480 | Tempo de expiracao do token JWT (minutos) |
| `ADMIN_NAME` | `Admin` | Nome do administrador inicial |
| `ADMIN_EMAIL` | `admin@localhost` | Email do administrador inicial. **Trocar em producao!** |
| `ADMIN_PASSWORD` | `admin` | Senha do administrador inicial. **Trocar em producao!** |

### Persistencia

Jobs e usuarios sao persistidos em SQLite (`jobs.sqlite3` dentro de `OCR_TMP_DIR`). Ao reiniciar, jobs que estavam em `processing` sao recuperados como `queued` e re-enfileirados automaticamente.

A limpeza por TTL remove arquivos (working_dir) de jobs finalizados mais antigos que `JOB_RETENTION_SECONDS`, preservando os registros no banco de dados.

### Endpoints

Publicos:
- `GET /health` -- retorna `{"status":"ok"}`
- `GET /readiness` -- verifica diretorio temporario e banco SQLite; retorna 503 se indisponivel
- `GET /login` -- pagina de login

Autenticacao:
- `POST /auth/login` -- login com `{"email", "password"}`, retorna JWT em cookie e body
- `POST /auth/logout` -- limpa cookie JWT
- `GET /auth/me` -- retorna usuario autenticado

Protegidos (JWT ou API_KEY):
- `GET /metrics` -- contagens de jobs por status
- `POST /api/jobs` -- upload de PDF para OCR
- `POST /api/jobs/batch` -- upload em lote
- `GET /api/jobs/{job_id}` -- status de um job
- `GET /api/jobs/{job_id}/download` -- download do resultado
- `GET /api/jobs/download-batch` -- download em ZIP
- `POST /api/jobs/clear-queue` -- limpar fila

Admin (JWT apenas):
- `GET /admin/users` -- tela de gestao de usuarios
- `GET /api/users` -- listar usuarios (JSON)
- `POST /api/users` -- criar usuario com `{"name", "email", "password"}`
