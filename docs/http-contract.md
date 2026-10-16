# Contrato HTTP v1

Entrega OCR-AM-04, atividade LPM `hkKLY54xl4fHElZsRNdK`. O contrato atual tem
42 operações sob `/api/v1`, incluindo 12 do catálogo local, versão de contrato `1.6.0`, após a AM-05 de
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

Esta entrega L2a expõe a ação no HTTP local. A ferramenta MCP e a rota no
gateway OAuth ficam para os próximos incrementos; não há anúncio de suporte
remoto, em clientes ou em produção. A vinculação efetiva no DocumentService
ainda não foi integrada, portanto esta entrega não garante o bloqueio de novas
associações de documentos de ponta a ponta.

## Validação

`tests/test_http_contract.py` valida respostas reais e exemplos contra schemas
JSON do OpenAPI; as 12 operações iniciais têm testes de troca HTTP neste módulo,
e as cinco operações de upload/admissão em `tests/test_upload_admission.py`.
Cobre 401/403/404/405/409/422/500, PDF inteiro,
Range 206/400/416, ausência de valores privados, request_id, cache e export sem
runtime. A suíte de identidades mantém a autorização real HTTP/MCP.
`tests/test_catalog_http.py` acrescenta o inventário exato das 12 operações de
catálogo e casos de depreciação sobre ASGI e SQLite reais: recibos, ciclo de
vida, CAS, replay persistido, integridade, limites e isolamento. O inventário
MCP permanece independente, com 11 ferramentas de catálogo (31 no total).

Referências consultadas em 15/10/2026:
[respostas adicionais do FastAPI](https://fastapi.tiangolo.com/advanced/additional-responses/)
e [handlers de erro do FastAPI](https://fastapi.tiangolo.com/tutorial/handling-errors/).
