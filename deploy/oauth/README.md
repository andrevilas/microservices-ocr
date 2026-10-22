# OAuth e HTTPS remoto: candidato AM-13

Este pacote cria uma instalação isolada: Keycloak, PostgreSQL e um único processo
OCR. Não reutiliza os volumes nem altera a instância atual do TrueNAS. O laboratório
executável está em `scripts/verify_remote_oauth.py`; o Compose precisa de validação
em um engine Docker disponível antes da implantação. Não há certificado, senha,
usuário real ou hostname publicado neste diretório.

## Endereços propostos

- `https://ocr-auth.andre.goiania.br/realms/ocr`: emissor OAuth/OIDC.
- `https://ocr-mcp.andre.goiania.br/mcp`: resource e endpoint MCP.
- A interface existente `ocr.andre.goiania.br` continua sendo uma instalação distinta.

São propostas baseadas no domínio documentado do projeto, não registros DNS criados.
É necessário confirmar o destino do candidato, emitir certificados válidos e
configurar as rotas no proxy/túnel. DNS sozinho não publica o serviço no Cloudflare
Tunnel. NPM deve verificar o certificado e enviar SNI e Host do hostname upstream.
Não habilitar confiança genérica em `X-Forwarded-*`: os backends usam TLS e ignoram
esses cabeçalhos. Só o proxy autorizado deve alcançar as portas dos containers.

## Configuração revisável

1. Selecione `OCR_IMAGE` do commit revisado, `KEYCLOAK_IMAGE` (base testada 26.8.1)
   e uma imagem PostgreSQL compatível, com patches revisados e digest fixado.
   Para esta montagem de dados, use PostgreSQL 17; outras versões podem exigir
   outro caminho de volume. Não publicar uma tag mutável sem registrar seu digest.
2. Em diretório privado absoluto fora do Git, crie os arquivos definidos em
   `secrets` no Compose, com ACL somente para o operador/runtime. Senhas distintas,
   aleatórias, sem eco em terminal. Os dois consumidores de `introspection-secret`
   devem receber o mesmo valor. Certificados devem conter os SANs dos hosts.
3. Copie `clients.example.json`. Cadastre callbacks exatos observados em cada
   aplicativo. O exemplo é somente para laboratório; não representa configuração
   validada de Claude, Codex, ChatGPT ou Antigravity. Gere um arquivo novo:

   ```powershell
   python -m app.oauth_realm --resource-origin https://ocr-mcp.andre.goiania.br --clients C:\private\clients.json --output C:\private\ocr-realm.json
   ```

4. Copie `remote.example.json`, ajuste URLs e `allowed_clients`. Vincule cada
   `sub` emitido pelo realm ao ID do usuário OCR e seu teto de escopos:

   ```json
   "subjects": {"UUID-REAL-DO-KEYCLOAK": {"user_id": 1, "scopes": ["jobs:read", "artifacts:read"]}}
   ```

   Não vincular pelo e-mail. Um mapa vazio permite discovery, mas nenhum acesso.
   Remover um vínculo requer reiniciar este processo; remover o usuário local é
   verificado em cada chamada. Para revogar imediatamente, encerrar/revogar a sessão
   no provedor. Administradores locais continuam sem privilégios administrativos
   pela superfície remota. `ca_file` é opcional e absoluto, para uma CA privada
   explicitamente confiável; clientes públicos precisam de TLS publicamente válido.
5. Defina `PRIVATE_DIR`, `REALM_FILE`, `REMOTE_CONFIG_FILE`, `IDENTITY_HOST` e as
   imagens em um arquivo de variáveis **sem segredos** fora do repositório. Execute
   `docker compose --env-file <arquivo> -f deploy/oauth/compose.yaml config --quiet`.
   Os binds são loopback por padrão. Ajustar interfaces/portas requer revisão de
   firewall e conectividade do proxy; nunca publicar backend/admin diretamente.
6. Depois de a implantação ser autorizada, iniciar database/identity, administrar
   o realm por acesso privado, criar usuários e verificar o issuer HTTPS. Só então
   iniciar OCR: falha de discovery impede que seus workers iniciem. O import do
   Keycloak não sobrescreve realm existente; alterações posteriores são operações
   administrativas explícitas. Retirar o bootstrap admin após criar o operador
   permanente; guardar recuperação e backup criptografado do banco.
7. Aplicar smoke com um usuário sem privilégios: discovery, PKCE S256, token
   direcionado ao resource, ferramenta MCP, PDF sintético, isolamento e revogação.
   O healthcheck TCP é somente liveness; não comprova identidade disponível.

## Reproduzir o laboratório local

Use uma distribuição Keycloak 26.8.1 isolada, conferida contra o checksum da
release oficial, Java compatível, OpenSSL e Chromium. O comando parte do checkout
com as dependências `dev` instaladas:

```powershell
python -m scripts.verify_remote_oauth --keycloak-dir C:\OCR\lab\keycloak-26.8.1 --java-home C:\Java --openssl C:\OpenSSL\bin\openssl.exe --browser 'C:\Program Files\Google\Chrome\Application\chrome.exe' --evidence C:\OCR\evidence\oauth-lab.json
```

O teste usa banco de identidade em memória, usuário e PDF sintéticos, portas
loopback e certificado efêmero. Clientes HTTP e OCR verificam explicitamente a CA
de teste; o navegador isolado aceita esse certificado autossinado. Exercita
discovery, consentimento, PKCE positivo/negativo, reuso de código, API/MCP,
audience incorreta, escopos e revogação. O callback HTTPS pertence ao cliente do
laboratório; não é uma rota de login adicionada ao OCR. O relatório não inclui
códigos, credenciais ou conteúdo dos tokens. Ao terminar, os processos próprios
são encerrados e os segredos temporários removidos. Não fornece certificado
público, publicação DNS, teste do Compose em execução ou homologação de apps.

## Limites do proxy de identidade

O hostname público de identidade deve permitir somente `/resources/`,
`/realms/ocr/.well-known/openid-configuration`, endpoints OIDC necessários
(`auth`, `token`, `certs`, `revoke`, `logout`, `userinfo`) e `/realms/ocr/login-actions/`.
Introspection pode ficar acessível apenas ao OCR por rede/rota privada compatível
com o issuer, mantendo TLS e autenticação confidencial. Bloquear `/admin/`,
`/realms/master/`, console de conta, registro dinâmico, métricas e portas de gestão.
Não usar apenas `hostname-admin` como controle de acesso. O proxy deve desativar logs
de query strings/corpos/cabeçalhos nas rotas OAuth: códigos e tokens são secretos.

## Comportamento da integração

- OAuth ocorre no Keycloak; OCR só publica metadados RFC 9728 e valida access tokens
  por introspection RFC 7662 autenticada, sem cache positivo. Não emite tokens OAuth.
- `iss`, `aud`, `client_id`, `sub`, `exp`, `nbf`, tipo Bearer, usuário existente e
  escopos são verificados. O teto local restringe os escopos concedidos pelo provedor.
  Não são aceitos cookies, JWTs locais nem credenciais `ocr_svc_` neste entrypoint.
- O mapper inclui o URL do resource e `ocr-resource` em `aud`: o primeiro é
  verificado pelo OCR; o segundo permite introspection pelo cliente confidencial
  nas versões recentes do Keycloak. Não desabilitar a checagem de audience do
  provedor para contornar uma configuração incorreta.
- TLS é obrigatório. Host e Origin têm allowlist exata; ausência de Origin é aceita
  para hosts nativos. CORS de navegador entre origens não está habilitado.
- Rotas privadas, UI, credenciais de serviço, OpenAPI e futuras rotas ficam fechadas
  por padrão. Discovery é público; `/mcp` e as operações de documentos exigem Bearer.
- Falha do provedor retorna 503; token inativo/inválido retorna 401 com discovery.
  A revogação afeta a próxima requisição, não cancela jobs já admitidos nem recolhe
  bytes já enviados em um download. Cancelar jobs é uma operação separada.
- Cada app precisa de registro e callbacks próprios. Este incremento usa clientes
  pré-registrados; não oferece registro dinâmico anônimo ou Client ID Metadata
  Documents. A aceitação desses registros por cada host será verificada na AM-16.
  Os seis clientes solicitados ainda não estão homologados ponta a ponta.

## Migração e reversão

O candidato usa novos volumes, portanto parar seus serviços e retirar as rotas
novas reverte a publicação sem tocar na instalação existente. Preservar os volumes
para investigação; não executar `down -v`. Nunca executar duas instâncias/workers
OCR sobre o mesmo diretório de dados. A migração dos usuários/dados existentes e
a coexistência da UI privada no mesmo runtime precisam de plano próprio antes de
substituir a produção. O pacote atual não realiza essa migração.

Referências: [MCP Authorization](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization),
[RFC 7662](https://www.rfc-editor.org/rfc/rfc7662.html),
[RFC 9728](https://www.rfc-editor.org/rfc/rfc9728.html),
[Keycloak containers](https://www.keycloak.org/server/containers),
[Keycloak reverse proxy](https://www.keycloak.org/server/reverseproxy).
Detalhe de audience: [migração Keycloak 26.6.2](https://www.keycloak.org/docs/26.8.0/upgrading/).

## Produção (22/10/2026)

O candidato de dados separados (Compose acima) **não** é usado em produção. O OCR de
produção é o próprio servidor de recursos: com `OCR_REMOTE_CONFIG_FILE`, o
`app.main:app` atende no mesmo processo dois hostnames.

- **Qualquer outro host:** a superfície local de sempre (UI, Bearer local, `/mcp` só em loopback).
- **`ocr-mcp.andre.goiania.br`:** a superfície `RemoteSurface`.
  - Só OAuth (introspection) e a allowlist `OPERATIONS`.
  - Sem UI, `/health` nem rotas RAG.
  - Exige o cabeçalho `X-OCR-Gateway`, que o NPM injeta, no lugar de TLS até o processo. O cabeçalho é removido antes do app.
- **Provedor fora:** a descoberta do provedor roda em segundo plano, com nova tentativa a cada 30 s. Enquanto o provedor estiver fora, só a superfície remota responde 503.

Caminho público:

| Hostname | Cadeia | Filtro |
| --- | --- | --- |
| `ocr-auth.andre.goiania.br` | Cloudflare Tunnel `truenas-npm` → NPM → Service `keycloak.ocr-oauth` (ClusterIP) | Discovery do realm `ocr` (também os aliases RFC 8414 com inserção de path), endpoints OIDC, `login-actions` e `/resources`. O resto, inclusive `/admin`, o realm `master`, o console de conta e DCR, responde 404 |
| `ocr-mcp.andre.goiania.br` | Cloudflare Tunnel `truenas-npm` → NPM → NodePort do OCR (31800) | `/mcp`, metadata RFC 9728 e `/api/v1/*`, com `X-OCR-Gateway` |

Arquivos de produção:
- **Manifests:** `k8s/ocr-oauth.yaml` (namespace `ocr-oauth`: PostgreSQL 17 e Keycloak 26.8.1, só ClusterIP, com limites de memória).
- **NPM:** `configure-npm-oauth-hosts.py`.
- **Clientes:** `clients.production.json`, com callbacks exatos a confirmar em cada app na AM-16.
- **Segredos:** em `/mnt/NVME/ocr-oauth/secrets` (root 0600). O OCR lê cópias em `/mnt/NVME/ocr-apps/config`, montado em `/etc/ocr-rag`.
- **Usuários:** são criados pelo admin REST interno.
- **`subjects` em `remote.json`:** liga cada `sub` do Keycloak a um usuário OCR explícito, nunca administrador.
