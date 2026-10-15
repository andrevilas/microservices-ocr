# API e MCP — fundação e identidades

Contrato HTTP versionado, exemplos e códigos: [HTTP v1](http-contract.md).

Rastreabilidade: OCR-AM-02 / LPM `AEHQJUb02uohT4MlVf8U`, projeto OCR Recognizer
`POzf83SdXU3HWL9KX9i2`. Plano completo e revisão na atividade
`lv2qAWLSJajcLawyvMdE`, com 18 atividades e 29 capacidades planejadas.

Estes incrementos entregam a fundação, identidades de serviço e consultas autenticadas. Uploads separados,
criação idempotente, texto por página, lotes, exportação, cancelamento ativo,
OAuth remoto e migração do painel continuam no backlog.
Não é a entrega do fluxo vertical completo OCR-AM-08.

## Operações disponíveis

| HTTP | Ferramenta MCP | Permissão |
|---|---|---|
| GET /api/v1/me | ocr_whoami | Identidade autenticada |
| GET /api/v1/jobs | ocr_list_jobs | jobs:read |
| GET /api/v1/jobs/{job_id} | ocr_get_job | jobs:read |
| GET /api/v1/metrics | ocr_get_metrics | jobs:read |
| GET /api/v1/jobs/{job_id}/artifact | ocr_get_artifact | jobs:read e artifacts:read |
| GET /api/v1/jobs/{job_id}/artifact/content | URL retornada por ocr_get_artifact | jobs:read e artifacts:read |

As duas interfaces chamam `JobApplication`, passando um `Principal` imutável.
O SDK MCP oficial está fixado em 2.3.0. `/openapi.json` declara Bearer, schemas,
operationId e erros da API v1. O download usa HTTP autenticado; PDFs não entram
no JSON das ferramentas. As cinco ferramentas são de leitura.

## Autenticação e compatibilidade

O piloto usa Bearer JWT de usuário emitido por `POST /auth/login` ou credencial
individual de serviço, enviado
explicitamente em `Authorization`. A identidade é validada no banco a cada
requisição. Expiração ausente, expirada ou inválida é rejeitada. As permissões
atuais do usuário são `jobs:read`, `artifacts:read`, `jobs:submit` e `jobs:cancel`.
Serviços recebem apenas os escopos explícitos da credencial.

Não há emissão de credenciais, login ou senha como ferramenta MCP. Tokens não
devem ser colocados em argumentos de ferramentas, URLs, logs ou exemplos
versionados. Provisionamento, rotação, revogação e escopos estão em
[Identidades de serviço](service-identities.md), entrega OCR-AM-03.

Cookies e a antiga chave global `X-API-Key` não autenticam API v1/MCP. As rotas
legadas preservam seus formatos, mas agora isolam proprietários em consultas,
downloads, ZIP, métricas e limpeza da fila. A chave global fica limitada ao
domínio reservado legacy:api-key; a AM-03 documenta essas mudanças de acesso.

Só o dono acessa cada job e artefato; até administradores
ficam sujeitos a essa regra. Jobs antigos sem dono não são públicos e não
recebem dono automaticamente. Objetos alheios, órfãos e inexistentes retornam
o mesmo 404 / `job_not_found`. Contagens também são restritas ao dono.

## HTTP

Exemplo em Python, recebendo um token existente pelo ambiente:

```python
import os
import httpx

headers = {"Authorization": "Bearer " + os.environ["OCR_ACCESS_TOKEN"]}
with httpx.Client(base_url="http://127.0.0.1:8000", headers=headers) as client:
    response = client.get("/api/v1/jobs", params={"limit": 20, "status": "completed"})
    response.raise_for_status()
    page = response.json()
    # Use page["next_cursor"] para continuar, mantendo o mesmo filtro.
```

Paginação: ordem ascendente por criação e ID, limite de 1 a 100, cursor opaco.
Alterar o status de jobs durante a paginação pode mudar a composição do filtro;
o cursor não representa um snapshot transacional de todo o catálogo.

Erros v1 usam `{"error":{"code":"...","message":"..."}}`, inclusive validação.
401 inclui `WWW-Authenticate: Bearer`; 403 indica falta de escopo; 409 indica
artefato indisponível. O ciclo de retenção/tombstones/410 será tratado na AM-11.

## MCP local

O endpoint é `http://127.0.0.1:8000/mcp`, Streamable HTTP sem estado de sessão
persistente. Cada requisição é autenticada, incluindo discovery e listagem.
O allowlist de Host/Origin aceita apenas loopback neste piloto; não desabilite
a proteção para expor remotamente. OAuth, hostname HTTPS e acesso remoto estão
na AM-13. O JWT local não representa conformidade OAuth para hosts remotos.

```python
import asyncio
import os
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

async def main():
    headers = {"Authorization": "Bearer " + os.environ["OCR_ACCESS_TOKEN"]}
    async with httpx2.AsyncClient(headers=headers) as http:
        transport = streamable_http_client("http://127.0.0.1:8000/mcp", http_client=http)
        async with Client(transport) as client:
            result = await client.call_tool("ocr_list_jobs", {"limit": 20})
            print(result.structured_content)

asyncio.run(main())
```

Testes usam o cliente oficial com enquadramento JSON-RPC/Streamable HTTP sobre
ASGI, tanto `mode="auto"` quanto `mode="legacy"`. Isso não é homologação de
ChatGPT, Claude ou de outros hosts. Nenhum subprocesso stdio adicional abre
o banco ou inicia outra fila neste incremento.

## Ciclo de vida e execução

Importar `app.main` ou os módulos de serviços não cria banco, diretório de
dados ou threads OCR. A inicialização preguiçosa é protegida por locks. O
lifespan da aplicação inicia uma fila e o gerenciador MCP e encerra ambos.
`create_app()` cria uma nova aplicação e um novo gerenciador MCP para testes;
o gerenciador do SDK não permite reiniciar a mesma instância.

Uma trava de arquivo mantida pelo sistema operacional impede dois servidores
de assumirem o mesmo diretório. Use uma réplica e `uvicorn --workers 1`.
O arquivo `.ocr-runtime.lock` permanece no disco; sua existência não significa
que está ocupado. Não apague ou substitua esse arquivo enquanto o servidor roda.
Não use essa estratégia em volumes de rede com semântica de locking não validada.

Se um worker ultrapassar o prazo de shutdown, o servidor acusa erro e mantém
a trava até o processo sair; não declara esse worker encerrado nem inicia
recuperação concorrente. Interrupção real dos subprocessos é parte da AM-10.

## Validação e limites

```bash
pip install -e '.[dev]'
pytest -q
pip check
```

Testes cobrem isolamento, download, órfãos, rejeição de chave global/cookie,
claims inválidos, paginação, escopos, OpenAPI, importação sem efeitos colaterais,
inicialização concorrente, trava entre processos, shutdown e paridade HTTP/MCP.
Use `OCR_TMP_DIR` apontando para dados descartáveis ao executar testes.
As correções dos testes de readiness e SQLite preservam as verificações e
permitem executá-las no Windows.

O motor OCR real, qualidade de PDFs escaneados, interoperabilidade de hosts
externos e migração de dados de produção não são comprovados por esses testes.
A instância TrueNAS não foi modificada.
