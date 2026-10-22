# API e MCP — fundação e identidades

Contrato HTTP versionado, exemplos e códigos: [HTTP v1](http-contract.md).

Rastreabilidade: OCR-AM-02 / LPM `AEHQJUb02uohT4MlVf8U`, projeto OCR Recognizer
`POzf83SdXU3HWL9KX9i2`. Plano completo e revisão na atividade
`lv2qAWLSJajcLawyvMdE`, com 18 atividades e 29 capacidades planejadas.

Estes incrementos entregam a fundação, identidades de serviço, consultas autenticadas,
[uploads persistentes e criação idempotente por HTTP](uploads.md),
[texto por página e relatórios persistentes](results.md),
[opções por job e capacidades verificadas](capabilities.md) e
[lotes/exportação assíncrona](batches-exports.md) e
[cancelamento/reprocessamento](job-control.md) e [retenção/exclusão](retention.md).
O [painel usa API v1 e restaura o histórico](workspace-ui.md), com
[matriz das 29 funcionalidades](operation-matrix.md) e [configurações para seis clientes MCP](mcp-clients.md).
O [candidato OAuth remoto com Keycloak](../deploy/oauth/README.md) acrescenta um
entrypoint HTTPS restrito, com discovery, introspection e vínculos explícitos de
usuário. A publicação pública, perfil/administração v1 e homologação completa dos
aplicativos continuam pendentes.
O [fluxo vertical MCP e cliente stdio](mcp-workflow.md), entrega OCR-AM-08,
permite preparar upload, submeter uma referência e recuperar texto e artefato.

## Operações disponíveis

| HTTP | Ferramenta MCP | Permissão |
|---|---|---|
| DELETE /api/v1/jobs/{id}/artifacts | ocr_delete_job_artifacts | artifacts:delete |
| POST /api/v1/jobs/{id}/cancel | ocr_cancel_job | jobs:cancel |
| POST /api/v1/jobs/cancel-batch | ocr_cancel_jobs | jobs:cancel |
| POST /api/v1/jobs/{id}/retry | ocr_retry_job | jobs:submit |
| GET /api/v1/me | ocr_whoami | Identidade autenticada |
| GET /api/v1/capabilities | ocr_get_capabilities | Identidade autenticada |
| POST /api/v1/upload-sessions | ocr_prepare_upload | jobs:submit |
| GET /api/v1/uploads/{upload_id} | ocr_get_upload | jobs:submit |
| POST /api/v1/jobs | ocr_submit_job | jobs:submit |
| POST /api/v1/batches | ocr_create_batch | jobs:submit |
| GET /api/v1/batches | ocr_list_batches | jobs:read |
| GET /api/v1/batches/{id} | ocr_get_batch | jobs:read |
| POST /api/v1/exports | ocr_create_export | jobs:read e artifacts:read |
| GET /api/v1/exports/{id} | ocr_get_export | jobs:read e artifacts:read |
| GET /api/v1/exports/{id}/content | URL retornada por ocr_get_export | jobs:read e artifacts:read |
| GET /api/v1/jobs | ocr_list_jobs | jobs:read |
| GET /api/v1/jobs/{job_id} | ocr_get_job | jobs:read |
| GET /api/v1/metrics | ocr_get_metrics | jobs:read |
| GET /api/v1/jobs/{job_id}/artifact | ocr_get_artifact | jobs:read e artifacts:read |
| GET /api/v1/jobs/{job_id}/text | ocr_get_text | jobs:read e artifacts:read |
| GET /api/v1/jobs/{job_id}/report | ocr_get_report | jobs:read e artifacts:read |
| GET /api/v1/jobs/{job_id}/artifact/content | URL retornada por ocr_get_artifact | jobs:read e artifacts:read |

### Catálogo (contextos e tipos documentais) — HTTP e MCP

Contrato atual: HTTP `1.8.0` com exatamente 66 operações (44 anteriores, incluindo
14 de catálogo, mais 15 de documentos e 7 de entrega RAG) e MCP `0.7.0` com exatamente 52 ferramentas:
20 legadas, 14 de catálogo, 12 de documentos e 6 de [entrega RAG](rag-api-mcp.md); 33 leituras e 19 escritas. A tabela
acima lista só o subconjunto legado (20 tools; histórico: 12 leituras e 8 escritas).
As 14 operações de catálogo (tag `Catalog`) estão implementadas em HTTP e MCP, e a
UI existente do catálogo continua disponível. Escopos:
leituras `contexts:read`; criar/atualizar `contexts:read` + `contexts:write`;
publicar `contexts:read` + `contexts:publish`. Tokens JWT legados (sem claims de
escopo) não recebem esses escopos (403).

A tabela abaixo detalha as 11 operações originais do catálogo (histórico daquele
corte, quando não havia tools MCP); as 3 restantes são:

- `POST /api/v1/document-types/{type_id}/versions/{version}/deprecate`
  (`ocr_deprecate_document_type_version`): `contexts:read` + `contexts:publish`;
- `GET /api/v1/context-templates` (`ocr_list_context_templates`): `contexts:read`;
- `POST /api/v1/context-templates/{template_id}/clone`
  (`ocr_clone_context_template`): `contexts:read` + `contexts:write`.

Detalhes em [HTTP v1](http-contract.md).

| HTTP | operationId |
|---|---|
| GET/POST /api/v1/contexts | ocr_list_contexts / ocr_create_context |
| GET/PUT /api/v1/contexts/{context_id} | ocr_get_context / ocr_update_context |
| GET/POST /api/v1/contexts/{context_id}/types | ocr_list_document_types / ocr_create_document_type |
| GET/PUT /api/v1/document-types/{type_id} | ocr_get_document_type / ocr_update_document_type |
| GET/POST /api/v1/document-types/{type_id}/versions | ocr_list_document_type_versions / ocr_publish_document_type |
| GET /api/v1/document-types/{type_id}/versions/{version} | ocr_get_document_type_version |

POST exige `Idempotency-Key` (ASCII, 8–128): 201 na criação, 200 com o recibo
original no replay (`Idempotency-Replayed: true|false`); a mesma chave com outro
conteúdo retorna 409. PUT substitui o recurso inteiro com `expected_revision`
(compare-and-swap, sem retry automático; 409 em conflito). Publicar exige
`expected_revision` e `expected_context_revision`. Corpos JSON têm limite de 2 MiB
lido em streaming (413), `application/json` obrigatório (415) e o payload canônico
tem limite próprio (422). Listas retornam só resumos (`limit` 1–100, padrão 25,
cursor `after`). A versão publicada devolve o schema canônico no campo de fio
`schema`; editar contexto/tipo depois nunca altera versões publicadas. Recursos de
outro dono, inclusive de administradores, retornam 404.

### Documentos e metadados

Quinze rotas HTTP e doze ferramentas MCP (10 leituras, salvar e aprovar) cobrem texto
canônico e revisões de metadados; escopos adicionais `metadata:read`,
`metadata:write` e `metadata:approve`. Matriz completa, limites e semântica de CAS e
idempotência: [metadados documentais](document-metadata-api.md). UI de metadados,
enriquecimento por LLM, XMP, PDF/A final, exportação e RAG externo continuam
pendentes.

As duas interfaces chamam `JobApplication`, passando um `Principal` imutável.
O SDK MCP oficial está fixado em 2.3.0. `/openapi.json` declara Bearer, schemas,
operationId e erros da API v1. O download usa HTTP autenticado; PDFs não entram
no JSON das ferramentas. Entre as 20 ferramentas legadas (subconjunto histórico),
doze são de leitura; preparação de upload,
submissão, criação de lote, exportação, retry, exclusão de arquivos e os dois cancelamentos são mutações (8). No total de 52 ferramentas, 29 são leituras e 19 escritas. Admissão e encaminhamento à fila usam o mesmo serviço
em HTTP e MCP.

## Autenticação e compatibilidade

O piloto usa Bearer JWT de usuário emitido por `POST /auth/login` ou credencial
individual de serviço, enviado
explicitamente em `Authorization`. A identidade é validada no banco a cada
requisição. Expiração ausente, expirada ou inválida é rejeitada. As permissões
atuais do usuário incluem `jobs:read`, `artifacts:read`, `jobs:submit`, `jobs:cancel` e `artifacts:delete`
(escopos de catálogo e metadados: ver seções próprias). Os contratos Bearer locais
são distintos do candidato a gateway remoto OAuth; não há afirmação geral de OAuth disponível.
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

Erros v1 usam error.code, error.message, error.details e request_id, inclusive validação.
401 inclui `WWW-Authenticate: Bearer`; 403 indica falta de escopo; 409 indica
artefato não produzido ou conflito de admissão. Upload expirado retorna 410;
arquivos expirados/excluídos de jobs também retornam 410, com tombstone persistido.

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
ASGI e TCP real, além de subprocesso stdio, tanto `mode="auto"` quanto
`mode="legacy"`. Isso não é homologação de ChatGPT, Claude ou de outros hosts.
O subprocesso stdio apenas chama a API autenticada; não abre banco ou inicia fila.

## Ciclo de vida e execução

Importar `app.main` ou os módulos de serviços não cria banco, diretório de
dados ou threads OCR. A inicialização preguiçosa é protegida por locks. O
lifespan da aplicação inicia fila, worker de exportação, coletor de retenção
e gerenciador MCP e os encerra no shutdown.
`create_app()` cria uma nova aplicação e um novo gerenciador MCP para testes;
o gerenciador do SDK não permite reiniciar a mesma instância.

Uma trava de arquivo mantida pelo sistema operacional impede dois servidores
de assumirem o mesmo diretório. Use uma réplica e `uvicorn --workers 1`.
O arquivo `.ocr-runtime.lock` permanece no disco; sua existência não significa
que está ocupado. Não apague ou substitua esse arquivo enquanto o servidor roda.
Não use essa estratégia em volumes de rede com semântica de locking não validada.

Se um worker ultrapassar o prazo de shutdown, o servidor acusa erro e mantém
a trava até o processo sair; não declara esse worker encerrado nem inicia
recuperação concorrente. A [AM-10](job-control.md) controla a interrupção dos subprocessos.

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
