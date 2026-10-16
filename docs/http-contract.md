# Contrato HTTP v1

Entrega OCR-AM-04, atividade LPM `hkKLY54xl4fHElZsRNdK`. O contrato atual tem
44 operações sob `/api/v1`, incluindo 14 do catálogo local, versão de contrato `1.6.0`, após a AM-05 de
[uploads persistentes e admissão idempotente](uploads.md) e a AM-06 de
[texto por página e relatórios](results.md) e AM-07 de
[opções e capacidades](capabilities.md) e AM-09 de [lotes/exportação](batches-exports.md). A AM-10 acrescenta [cancelamento e reprocessamento](job-control.md), e a AM-11 [retenção e exclusão](retention.md). Administração ampla continua no backlog.

[openapi-v1.json](openapi-v1.json) inclui apenas operações v1 e seus schemas.
`/openapi.json` e `/docs` também contêm as rotas legadas, com contratos separados.

```powershell
python -m app.export_openapi docs/openapi-v1.json
python -m app.export_openapi docs/openapi-v1.json --check
```

Exportar/verificar não cria banco, fila ou worker. `--check` não escreve e falha
se o arquivo estiver ausente ou desatualizado. O OpenAPI da aplicação é a fonte;
atualize o export junto de cada novo endpoint.

## Segurança e compatibilidade

`PrincipalBearer` declara JWT de usuário ou credencial opaca individual de
serviço. Não anuncia cookie, chave global ou OAuth como alternativas.
`x-required-scopes` informa permissões, sem declarar scopes OAuth.
`x-human-admin-required` identifica operações do administrador humano.
A autorização continua sendo aplicada pelo servidor, inclusive por proprietário.

IDs de operação são estáveis e únicos. Alterações compatíveis em v1 podem
adicionar endpoints e campos opcionais. Consumidores devem ignorar campos
desconhecidos nas respostas e tratar códigos desconhecidos pelo status HTTP.
Remover campos, mudar tipos ou significados exige nova versão ou migração
explicitamente documentada. Entrada rejeita campos extras onde declarada
`additionalProperties: false`. Escopos, tamanhos, paginação, validade e exemplos
constam dos schemas/operações. Listagens por cursor são leituras vivas, não snapshots.

Códigos ficam em `snake_case` minúsculo, preservando a fundação; os nomes em
maiúsculo do plano eram propostas. A versão HTTP é independente do protocolo MCP.

## Respostas e erros

Toda resposta v1 recebe `X-Request-ID` gerado pelo servidor e
`Cache-Control: no-store`. O X-Request-ID do cliente é ignorado.
Em erros, `request_id` no corpo corresponde ao cabeçalho:

```json
{
  "error": {
    "code": "validation_error",
    "message": "Invalid request parameters.",
    "details": [{"location": ["query", "limit"], "code": "greater_than_equal"}]
  },
  "request_id": "00000000000000000000000000000000"
}
```

`details` contém no máximo 20 indicações de localização/tipo; nunca replica
valores, corpos, contexto interno do validador ou stderr do OCR. Nomes de
propriedades extras são omitidos. Erros sem detalhe usam `[]`.
Use status e `code` para controle do cliente, não o texto de `message`.

| Status | Códigos / situação |
|---|---|
| 400 | invalid_request: Range malformado |
| 401 | authentication_required; inclui WWW-Authenticate: Bearer |
| 403 | insufficient_scope, admin_required |
| 404 | job_not_found, identity_not_found, route_not_found |
| 405 | method_not_allowed; inclui Allow |
| 409 | artifact_unavailable |
| 410 | artifacts_expired, artifacts_deleted, export_source_removed |
| 416 | invalid_range; preserva Content-Range |
| 422 | validation_error, invalid_cursor, invalid_limit, invalid_identity_request |
| 500 | internal_error: mensagem genérica, sem texto da exceção |

Jobs inexistentes e não visíveis têm o mesmo 404. Artefato não pronto retorna
metadado com `available: false`; download retorna 409. Após expiração/exclusão,
o metadado inclui `retention` e conteúdo retorna 410. Downloads concluídos
retornam PDF binário com 200, ou 206 para Range. Erros continuam JSON. Falha
de transporte após começar o arquivo encerra a conexão; bytes já enviados
não podem ser substituídos por JSON.

URLs v1 usam caminhos exatos, sem redirecionamento por barra final. URLs
desconhecidas também recebem o envelope. MCP mantém os erros de protocolo e
resultados de ferramenta; não recebe envelope HTTP v1 dentro do JSON-RPC.
As rotas legadas mantêm seu próprio formato.

## Templates locais de contexto

`GET /api/v1/context-templates` (`ocr_list_context_templates`) exige
`contexts:read` e retorna `ContextTemplateList`: exatamente três registros
completos, estáticos e em ordem fixa. Não há filtros, paginação ou criação
implícita de contextos, tipos ou versões. O catálogo é global e igual para os
proprietários autorizados.

`POST /api/v1/context-templates/{template_id}/clone`
(`ocr_clone_context_template`) exige `contexts:read` e `contexts:write`;
`contexts:publish` não é necessário. O identificador usa ASCII minúsculo,
sintaxe `^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$` e no máximo 64 caracteres; não é UUID.
Um identificador válido desconhecido retorna 404 `context_template_not_found`.
O corpo fechado `TemplateCloneInput` contém somente `expected_version`, inteiro
estrito positivo, e `expected_sha256`, exatamente 64 caracteres hexadecimais
minúsculos. Não aceita proprietário: a identidade autenticada determina a posse.
`Idempotency-Key` é obrigatório, com as regras existentes do catálogo
(8–128 caracteres ASCII). Os dois escopos são verificados antes de ler o corpo,
com limite de 2 MiB durante o streaming.

O serviço cria atomicamente um contexto e seus tipos como rascunhos editáveis,
todos na revisão 1, sem publicar versões. Retorna 201 com
`Idempotency-Replayed: false`, ou 200 com `true` e o recibo original persistido
`TemplateCloneReceipt`. O recibo identifica a edição/hash do template, o contexto,
os tipos e a data UTC; não representa o estado atual dos rascunhos.
Replay não desfaz edições nem publicações e continua válido quando a quota está
cheia. Uma chave nova cria outra cópia, sujeita à quota.

Edição/hash divergentes retornam 409; outra requisição sob a mesma
identidade/chave retorna 409 `idempotency_conflict`. Quota excedida retorna 429;
corrupção ou indisponibilidade de armazenamento retorna 503, sem conteúdo
interno. A política e a transação pertencem ao serviço existente. Não há retry
automático: após resultado incerto, repita a mesma chave e o mesmo corpo.
Os envelopes, `Cache-Control: no-store`, `X-Request-ID` e a política existente
de concessão de escopos a serviços continuam aplicáveis.

Templates são pontos de partida editáveis, sem aprovação de conformidade
documental. Esta etapa entrega apenas HTTP local: o inventário passa a 44
operações HTTP e permanece com 32 ferramentas MCP. Templates via OAuth/MCP
vêm na próxima etapa; o ciclo de vida de versões já tem OAuth/MCP aceitos.
Não representa conclusão de UI, integração com DocumentService, homologação
de clientes externos ou implantação em produção.

## Catálogo e ciclo de vida de versões

`POST /api/v1/document-types/{type_id}/versions/{version}/deprecate`
(`ocr_deprecate_document_type_version`) exige `contexts:read` e
`contexts:publish`. O corpo fechado contém apenas
`expected_lifecycle_revision`, inteiro estrito entre 1 e `2**31 - 1`.
A versão no caminho deve ser um inteiro entre 1 e `2**63 - 1`; não há `latest`.
`Idempotency-Key` é obrigatório, com as mesmas regras das demais escritas
idempotentes do catálogo. Os escopos são verificados antes da leitura do corpo,
limitado a 2 MiB.

A ação usa compare-and-swap do ciclo de vida e retorna **200** tanto na primeira
execução (`Idempotency-Replayed: false`) quanto na repetição persistida
(`true`). O recibo contém somente `type_id`, `version`, `schema_sha256` e
`lifecycle`, com estado `deprecated`, revisão 2 e `changed_at` em UTC.
A mesma identidade/chave e requisição reproduzem exatamente o recibo original,
inclusive após reinício ou edições posteriores, antes de verificar novamente
CAS. Reutilizar a chave com outra requisição retorna 409 `idempotency_conflict`.
Uma chave nova com revisão obsoleta retorna 409
`type_lifecycle_revision_conflict`; com revisão atual de uma versão já
depreciada, 409 `type_version_already_deprecated`.
Versões inexistentes ou de outro proprietário retornam o mesmo 404
`type_version_not_found`, inclusive para administrador. Corrupção do snapshot
ou falha operacional de armazenamento retorna 503 `storage_unavailable`, sem
expor conteúdo interno.

O recibo de publicação (`TypeVersionSummary`) permanece histórico e estável,
sem `lifecycle`, inclusive em replay após depreciação. GET e listagem de versões
mostram o ciclo de vida atual junto do envelope imutável. Versões depreciadas
continuam legíveis e listadas; bytes canônicos, schema e hash não mudam.
Depreciar não libera quota, exclui versões ou permite reativação. Uma nova
publicação nasce com ciclo de vida `published`, revisão 1.

A ação está exposta no HTTP local, na ferramenta MCP
`ocr_deprecate_document_type_version` e na rota explícita do gateway OAuth
`POST /api/v1/document-types/{type_id}/versions/{version}/deprecate`, com os
mesmos requisitos de escopo, proprietário, CAS e replay. Com os templates locais, o contrato passa a 44 operações HTTP; o MCP anuncia 32 ferramentas, sendo 12 do catálogo e
20 legadas. A matriz SDK validada cobre HTTP e stdio nos modos auto e legacy;
a validação em aplicações desktop/CLI reais, com emissor externo e DNS/TLS,
continua pendente. Isso não anuncia implantação em produção nem conclusão de
Contexts ou UI. A vinculação efetiva no DocumentService ainda não foi
integrada, portanto esta entrega não garante o bloqueio de novas associações
de documentos de ponta a ponta.

## Validação

`tests/test_http_contract.py` valida respostas reais e exemplos contra schemas
JSON do OpenAPI; as 12 operações iniciais têm testes de troca HTTP neste módulo,
e as cinco operações de upload/admissão em `tests/test_upload_admission.py`.
Cobre 401/403/404/405/409/422/500, PDF inteiro,
Range 206/400/416, ausência de valores privados, request_id, cache e export sem
runtime. A suíte de identidades mantém a autorização real HTTP/MCP.
`tests/test_catalog_http.py` acrescenta o inventário exato das 14 operações de
catálogo e casos de depreciação sobre ASGI e SQLite reais: recibos, ciclo de
vida, CAS, replay persistido, integridade, limites e isolamento.
`tests/test_context_templates_http.py` propõe cobertura HTTP/SQLite real para
listagem sem escrita, clone de cada template, revisões iniciais, replay após
edição/publicação, conflitos, entradas inválidas, escopos antes do corpo,
limite em streaming, posse e isolamento do provider. Usa override explícito
de principal; não comprova autenticação por credenciais reais.
As verificações gerais de inventário HTTP passam de 42 para 44.
A execução das suítes afetadas e a regeneração/verificação offline de
`docs/openapi-v1.json` cabem ao coordenador; esta proposta não executou testes.
O inventário
MCP permanece independente, com 12 ferramentas de catálogo (32 no total).
`tests/test_catalog_lifecycle_mcp_vertical.py` verifica
recibos, replay, conflitos, preservação do snapshot e autorização por clientes
SDK reais HTTP/stdio, em auto e legacy. A validação independente de 16/10/2026
executou oito suítes: 150 testes passaram e três verificações existentes de
aplicativos reais foram ignoradas por falta dos respectivos binários no
contêiner de testes. Isso não substitui a homologação desses aplicativos.

Referências consultadas em 15/10/2026:
[respostas adicionais do FastAPI](https://fastapi.tiangolo.com/advanced/additional-responses/)
e [handlers de erro do FastAPI](https://fastapi.tiangolo.com/tutorial/handling-errors/).
