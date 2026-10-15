# Identidades de serviço e migração de acesso

Entrega OCR-AM-03, atividade LPM `qR78ZWy1DKVfNlYvOaLI`.

Cada integração possui um proprietário `service:<id>` independente dos usuários
humanos `user:<id>`. As credenciais de um mesmo serviço compartilham os seus
documentos; dois serviços distintos nunca compartilham acesso implicitamente.
Administrar usuários ou provisionar integrações não concede leitura documental
ao token humano do administrador.

## Provisionamento HTTP

Somente um **JWT Bearer de usuário com papel admin validado no banco** pode
executar as operações abaixo. Cookie de navegador, chave global e credencial
de serviço não podem administrar identidades, mesmo que o pedido tente
fornecer campos como role, owner ou escopos administrativos.

| Método e caminho | Resultado |
|---|---|
| POST /api/v1/service-principals | Cria serviço, recebe name |
| GET /api/v1/service-principals | Lista metadados, sem credenciais |
| POST /api/v1/service-principals/{id}/credentials | Emite uma credencial individual |
| GET /api/v1/service-principals/{id}/credentials | Lista metadados; nunca devolve o segredo ou hash |
| DELETE /api/v1/service-principals/{id}/credentials/{credential_id} | Revoga de forma idempotente |
| POST /api/v1/service-principals/{id}/disable | Desabilita permanentemente o serviço e bloqueia todas as credenciais |

Listagens aceitam `limit` de 1 a 100 e `after` com o último ID recebido; ordenam
por ID. Não são snapshots de inserções concorrentes. Nenhuma dessas operações
é uma ferramenta MCP de emissão/recebimento de segredo. A gestão mais ampla
de perfil e administração continua na AM-14.

Corpo de emissão:

```json
{
  "name": "integração local",
  "scopes": ["jobs:read", "artifacts:read", "jobs:submit"],
  "expires_in_seconds": 2592000
}
```

Os escopos são uma lista explícita sem duplicatas: `jobs:read`, `jobs:submit`, `jobs:cancel`,
`artifacts:read` e `artifacts:delete`. A exclusão de arquivos exige concessão
explícita; credenciais existentes não recebem esse escopo automaticamente.
A validade padrão é de
30 dias; valores aceitos vão de 60 segundos a 365 dias. O segredo opaco é
retornado **uma única vez**, junto dos metadados; a resposta usa `Cache-Control:
no-store`. Guarde-o no gerenciador de segredos do consumidor. A API não oferece
recuperação do segredo: emita substituta e revogue a antiga.

Exemplo de provisionamento sem imprimir credenciais:

```python
import os
import httpx

with httpx.Client(base_url="http://127.0.0.1:8000",
                  headers={"Authorization": "Bearer " + os.environ["OCR_ADMIN_ACCESS_TOKEN"]}) as client:
    response = client.post("/api/v1/service-principals", json={"name": "importador local"})
    response.raise_for_status()
    service_id = response.json()["id"]
    response = client.post(f"/api/v1/service-principals/{service_id}/credentials",
                           json={"name": "piloto", "scopes": ["jobs:read", "jobs:submit", "artifacts:read"]})
    response.raise_for_status()
    credential = response.json()
    # Entregue credential["token"] ao armazenamento seguro do consumidor.
    # Não imprima o corpo da resposta, nem coloque o token em argumentos MCP.
```

O serviço usa `Authorization: Bearer <credencial>` na API e na conexão MCP
local. O protocolo de uso MCP é o mesmo descrito em [API e MCP](api-mcp.md).
Isto é o piloto com credencial individual; não implementa OAuth discovery/PKCE.

## Escopos vigentes

| Escopo | Permite, somente para o proprietário |
|---|---|
| jobs:read | Listar/consultar jobs e métricas |
| artifacts:read | Consultar/download de artefato; também exige jobs:read |
| jobs:submit | Submeter PDF pela rota multipart legada, inclusive lote |
| jobs:cancel | Retirar seus jobs pendentes da fila legada |

O contrato inicial consolidou os nomes `jobs:*` e `artifacts:read`; os nomes
`ocr:*` do plano eram propostas. Usuários humanos autenticados mantêm os quatro
escopos para seus próprios dados. Não há curinga, escopo administrativo delegável
ou leitura automática de documentos alheios. Um token só de submissão pode
receber o ID criado, mas precisa de jobs:read para consultá-lo.

As mutações `/api/v1/uploads` e `/api/v1/jobs` foram entregues na AM-05;
consulte [uploads persistentes](uploads.md). Ferramentas MCP de submissão
permanecem na AM-08. Serviços já podem enviar por HTTP v1 e acompanhar por MCP.

## Armazenamento e revogação

`identities.sqlite3` fica em `OCR_TMP_DIR`; inclui principals, credenciais e
auditoria de criação, emissão, revogação e desativação. A credencial contém
32 bytes aleatórios gerados por `secrets.token_urlsafe(32)`. Somente SHA-256
do token é persistido, e a comparação usa `secrets.compare_digest`.

Cada autenticação lê a situação atual e registra `last_used_at`. Revogação,
desativação e autenticação são serializadas por transações SQLite. A próxima
requisição após a revogação é recusada, inclusive na mesma conexão MCP. Uma
requisição já autenticada e em andamento não é cancelada retroativamente.
Eventos de auditoria registram ator e IDs; não contêm segredo nem hash.

Para rotação: emita uma segunda credencial para **o mesmo serviço**, atualize
o consumidor, confirme o uso e revogue a anterior. Criar outro serviço muda
o proprietário e não transfere os documentos. A desativação não possui
reativação neste incremento e não exclui jobs/artefatos.

## Mudanças de acesso na API legada

- Bearer explícito tem precedência. Token inválido não usa cookie ou chave
  global como alternativa. Cookies continuam humanos para compatibilidade do painel.
- Toda criação pública agora grava `owner_principal_id`; o chamador não escolhe
  o proprietário no corpo da requisição.
- Na migração aditiva, jobs com owner_user_id conhecido são mapeados para
  user:<id>. O vínculo humano antigo é preservado.
- Jobs antigos sem dono continuam sem dono e são negados em **ambas** as APIs.
  A atribuição posterior exige inventário e decisão explícita; não existe
  mecanismo de reivindicação por conhecer o ID.
- A chave global pode criar/consultar/cancelar apenas seu domínio reservado
  `legacy:api-key`, sem acesso a humanos, serviços ou órfãos. Não autentica
  API v1/MCP e deve ser substituída após inventário dos consumidores.
- Métricas, ZIP e limpeza da fila respeitam o mesmo proprietário. Limpar fila
  sem proprietário explícito deixa de ser uma operação válida.

Essas são mudanças intencionais de segurança, não compatibilidade integral
com o antigo acesso amplo. A migração só foi ensaiada sobre banco sintético.
Nenhum banco de produção foi aberto ou migrado. O ensaio de backup/rollback
e o inventário da produção permanecem na AM-17 antes de promoção.

## Testes e referências

`tests/test_service_identities.py` cobre dois usuários e dois serviços em HTTP,
API legada e SDK MCP (auto/legacy), órfãos, ZIP, métricas, cancelamento, escopos,
expiração, rotação, revogação, persistência, ausência de segredo nos registros,
permissões administrativas, concorrência e migração aditiva.

Referências consultadas em 15/10/2026: [Python secrets](https://docs.python.org/3/library/secrets.html)
e [OWASP REST Security](https://cheatsheetseries.owasp.org/cheatsheets/REST_Security_Cheat_Sheet.html).
