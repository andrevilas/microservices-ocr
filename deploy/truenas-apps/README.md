# TrueNAS Apps - Pilha Microservices OCR

Esta pasta registra a infraestrutura, estratégia e comandos para implantação, validação, backup e rollback do **microservices-ocr** (Fluxo OCR) no TrueNAS SCALE através do script de rollout automatizado.

---

## Estratégia de Implantação (Rollout)

O **microservices-ocr** é executado como um aplicativo TrueNAS SCALE (`ix-chart`) sob o nome de release `ocr-recognizer` no namespace `ix-ocr-recognizer`.

A promoção regular segue o mesmo padrão operacional usado no Docflow:

```text
git commit -> git push origin main -> CI -> Publish OCR Image/GHCR -> TrueNAS preflight -> backup -> rollout -> validação HTTP
```

Use sempre uma tag imutável `sha-<commit-curto>` no TrueNAS. A tag `latest` só deve servir para inspeção ou teste manual explícito.

A estratégia de rollout segue o modelo de promoção de imagens com validação ativa e preflight checks, dividida nas seguintes etapas:
1. **Preflight**:
   - Valida a versão do TrueNAS SCALE e o estado dos nós do cluster Kubernetes (k3s).
   - Verifica a existência e permissões do dataset ZFS do banco de dados/dados persistentes (`ocr-apps/data`).
   - Garante que a tag de imagem informada é imutável (bloqueando a tag `latest` a menos que `--allow-latest` seja passado explicitamente).
   - Valida se a imagem pode ser baixada com sucesso (via pod de validação temporário se houver `imagePullSecrets` configurado, ou via `k3s ctr` caso contrário).
2. **Backup**:
   - Realiza o backup compactado (`tar.gz`) de todos os dados do diretório de armazenamento persistente (banco SQLite `users.sqlite3`, `jobs.sqlite3` e arquivos de jobs).
   - Grava o estado atual da release TrueNAS (`release-state.json`) e manifestos de backup com checksum SHA256 para auditoria.
3. **Apply (Promoção de Imagem)**:
   - Atualiza **apenas** a tag e repositório da imagem no release do TrueNAS SCALE via chamada de API interna (`midclt chart.release.update`), preservando inteiramente as outras configurações existentes.
4. **Validation (Validação Ativa)**:
   - Aguarda até que o status da release no TrueNAS esteja `ACTIVE`.
   - Executa `k3s kubectl rollout status` para aguardar a prontidão do deployment do pod.
   - Faz chamadas HTTP recursivas (NodePort local e URL Pública) no endpoint `/health` para atestar a integridade e disponibilidade do serviço.

---

## Variáveis de Ambiente e Configurações

O script `rollout.py` aceita configurações via variáveis de ambiente ou por parâmetros de CLI.

| Variável | Descrição | Valor Padrão |
|---|---|---|
| `OCR_HOST` | Endereço IP do host TrueNAS / nó Kubernetes | `192.168.3.140` |
| `OCR_PUBLIC_URL` | URL pública de exposição da aplicação | `http://192.168.3.140:31800` |
| `OCR_IMAGE_REPOSITORY` | Repositório da imagem Docker | `ghcr.io/andrevilas/microservices-ocr` |
| `OCR_WEB_PORT` | Porta NodePort da aplicação exposta no TrueNAS | `31800` |
| `OCR_RELEASE_ROOT` | Diretório onde os reports de rollout serão salvos | `/mnt/NVME/ocr-apps/releases` |
| `OCR_BACKUP_ROOT` | Diretório raiz para armazenamento dos backups compactados | `/mnt/NVME/ocr-apps/backups` |
| `OCR_CURL_IP_VERSION` | Versão de IP usada pelo curl para validação HTTP (`4`, `6` ou `any`) | `4` |
| `OCR_TRUENAS_RELEASE` | Nome da release configurada no Apps | `ocr-recognizer` |
| `OCR_TRUENAS_NAMESPACE` | Namespace Kubernetes da aplicação | `ix-ocr-recognizer` |
| `OCR_TRUENAS_DEPLOYMENTS`| Lista de deployments a validar, separados por vírgula | `ocr-recognizer-ix-chart` |
| `OCR_TRUENAS_DATASET` | Dataset ZFS a ser verificado no preflight | `NVME/ocr-apps/data` |
| `OCR_DATA_DIR` | Caminho no host correspondente ao dataset persistente | `/mnt/NVME/ocr-apps/data` |
| `OCR_ROLLOUT_HTTP_ATTEMPTS`| Número de tentativas de checagem HTTP | `12` |
| `OCR_ROLLOUT_HTTP_RETRY_DELAY`| Intervalo em segundos entre tentativas de checagem HTTP | `5` |
| `OCR_ROLLOUT_RELEASE_ATTEMPTS`| Número de tentativas para aguardar a release voltar a `ACTIVE` | `90` |
| `OCR_IMAGE_PULL_POLICY` | Política de pull de imagem do Kubernetes | `IfNotPresent` |

---

## GHCR e pull de imagem

O workflow `.github/workflows/publish-image.yml` publica a imagem em:

```text
ghcr.io/andrevilas/microservices-ocr
```

Tags esperadas:

- `latest`, somente para a branch principal;
- `sha-<commit>`, recomendada para rollout;
- `v*`, quando houver tag semântica.

Antes de promover uma versão, valide que o manifest existe:

```bash
docker manifest inspect ghcr.io/andrevilas/microservices-ocr:sha-8509947
```

Se o pacote GHCR permanecer privado, o TrueNAS/k3s precisa de uma credencial de pull dedicada. Não usar token pessoal amplo. Criar um token com escopo mínimo de leitura de pacotes e aplicar o Secret no namespace do App gerenciado:

```bash
export GHCR_USERNAME='usuario-ghcr'
export GHCR_TOKEN='token-com-read-packages'
export GHCR_EMAIL='email-institucional@example.com'
export OCR_K8S_NAMESPACE='ix-ocr-recognizer'
export OCR_TRUENAS_RELEASE='ocr-recognizer'
deploy/truenas-apps/scripts/configure-ghcr-pull-secret.sh
```

O script:

- cria/atualiza o Secret Kubernetes `ghcr-pull-secret`;
- preserva os valores atuais do App TrueNAS `ocr-recognizer`;
- adiciona `imagePullSecrets: [{name: ghcr-pull-secret}]` ao release;
- valida o pull com um pod temporário usando `imagePullPolicy: Always`;
- remove o pod temporário ao final.

O `rollout.py` respeita essa configuração. Quando o release possui `imagePullSecrets`, o preflight cria um pod temporário no namespace do App para validar que o k3s consegue puxar a imagem privada do GHCR com a mesma credencial usada pelo deploy real. Quando não há `imagePullSecrets`, o preflight usa `k3s ctr images pull`, adequado para registry público ou imagem já acessível anonimamente.

---

## Exposição pública planejada

Subdomínio alvo:

```text
https://ocr.andre.goiania.br
```

Desenho recomendado, igual ao padrão validado no Docflow:

```text
ocr.andre.goiania.br -> Cloudflare Tunnel truenas-npm -> http://192.168.3.140:30021 -> Nginx Proxy Manager -> http://192.168.3.140:31800
```

No Nginx Proxy Manager, criar um Proxy Host dedicado:

| Campo | Valor |
|---|---|
| Domain Names | `ocr.andre.goiania.br` |
| Scheme | `http` |
| Forward Hostname / IP | `192.168.3.140` |
| Forward Port | `31800` |
| Websockets Support | habilitado |
| Block Common Exploits | habilitado |

No Cloudflare Tunnel `truenas-npm`, criar uma rota publicada:

```text
ocr.andre.goiania.br -> http://192.168.3.140:30021
```

Depois que o proxy/tunnel estiver criado, executar validação pública com:

```bash
curl -k -sS -o /tmp/ocr-health.json \
  -w 'ocr_public_health http=%{http_code} content=%{content_type}\n' \
  https://ocr.andre.goiania.br/health

curl -k -sS -o /tmp/ocr-login.html \
  -w 'ocr_public_login http=%{http_code} content=%{content_type}\n' \
  https://ocr.andre.goiania.br/login
```

Resultado esperado:

- `/health`: `200` com JSON `{"status":"ok"}`;
- `/login`: `200` com HTML da aplicação.

Quando a exposição pública estiver ativa, usar o domínio no rollout:

```bash
OCR_PUBLIC_URL=https://ocr.andre.goiania.br \
python3 deploy/truenas-apps/scripts/rollout.py validate
```

---

## Como Utilizar o Script de Rollout

O script deve ser executado no próprio shell do TrueNAS (ou ambiente com acesso ao comando `midclt` e `k3s`).

### 1. Instalação inicial / reconciliação da release

Quando a release `ocr-recognizer` ainda não existir no TrueNAS Apps, crie-a com o helper idempotente:

```bash
OCR_IMAGE_TAG=sha-749f6da \
python3 deploy/truenas-apps/scripts/apply-ix-chart-app.py
```

O helper cria os datasets:

- `NVME/ocr-apps/data`
- `NVME/ocr-apps/releases`
- `NVME/ocr-apps/backups`

Ele também configura o volume persistente em `/tmp/ocr-recognizer`, o NodePort `31800`, segredos de produção e o portal da aplicação. Se `ADMIN_PASSWORD` e `JWT_SECRET` não forem informados por ambiente, valores seguros são gerados e preservados em atualizações futuras da release.

### 2. Preflight (Verificação Prévia)
Valida o ambiente e a disponibilidade da nova imagem sem alterar o estado de produção.
```bash
python3 deploy/truenas-apps/scripts/rollout.py preflight --image-tag sha-e3b0c44
```

### 3. Backup Manual
Gera um backup avulso sob demanda dos dados persistentes (SQLite e arquivos de trabalho):
```bash
python3 deploy/truenas-apps/scripts/rollout.py backup
```

### 4. Deploy (Implantação Completa)
Executa a esteira completa: preflight -> backup -> atualização da imagem -> validação HTTP e de prontidão.
```bash
python3 deploy/truenas-apps/scripts/rollout.py deploy --image-tag sha-e3b0c44
```
*Caso necessite ignorar o backup (ex: migrações de teste), passe `--skip-backup --confirm-skip-backup`.*

### 5. Validação Manual
Roda as validações de prontidão da release, status do deployment e teste HTTP do serviço:
```bash
python3 deploy/truenas-apps/scripts/rollout.py validate
```

---

## Estratégia de Rollback

Se uma implantação falhar ou o serviço apresentar comportamento inesperado após o deploy, um rollback explícito e seguro deve ser efetuado.

### Procedimento de Rollback de Código
Para reverter a aplicação para uma versão anterior estável, execute o comando `rollback` passando a tag da imagem anterior (as tags de rollout devem ser imutáveis):
```bash
python3 deploy/truenas-apps/scripts/rollout.py rollback --image-tag sha-anterior
```
O comando executará:
1. A alteração da tag da imagem na API do TrueNAS para a versão especificada.
2. A validação de prontidão dos pods (`rollout status`).
3. O teste de integridade HTTP do endpoint `/health`.

Ao final, um report de rollback será gerado na pasta de releases (ex: `/mnt/NVME/ocr-apps/releases/rollback-YYYYMMDD-HHMMSS/`).

### Reversão de Dados (Restaurando Backup SQLite/dados persistentes)
Se o banco SQLite tiver sofrido corrupção ou alterações indesejadas, você pode restaurar o backup gerado no início da implantação.

1. Identifique o backup desejado no diretório `/mnt/NVME/ocr-apps/backups/<STAMP>/`.
2. Interrompa temporariamente a release no TrueNAS SCALE (para evitar conflito de escrita no SQLite):
   ```bash
   midclt call chart.release.scale "ocr-recognizer" {"replica_count": 0}
   ```
3. Remova/limpe os arquivos corrompidos no diretório persistente:
   ```bash
   rm -rf /mnt/NVME/ocr-apps/data/*
   ```
4. Descompacte o arquivo de backup de volta ao dataset persistente:
   ```bash
   tar -xzf /mnt/NVME/ocr-apps/backups/<STAMP>/ocr_data.tar.gz -C /mnt/NVME/ocr-apps/data/
   ```
5. Inicie a release novamente escalando os réplicas de volta para o padrão:
   ```bash
   midclt call chart.release.scale "ocr-recognizer" {"replica_count": 1}
   ```

---

## Estrutura de Evidências

Todas as execuções de `deploy` e `rollback` geram um subdiretório na pasta configurada em `--release-root`. Cada execução produz:
- `report.json`: Dados estruturados das checagens realizadas, comandos executados (com stderr/stdout), tags utilizadas e horários.
- `report.md`: Sumário formatado em Markdown com as evidências de sucesso ou falha da implantação.
- `previous-state.json`: Instantâneo do estado das releases TrueNAS antes da atualização da stack.
