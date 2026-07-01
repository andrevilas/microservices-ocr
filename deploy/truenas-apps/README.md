# TrueNAS Apps - Pilha Microservices OCR

Esta pasta registra a infraestrutura, estratégia e comandos para implantação, validação, backup e rollback do **microservices-ocr** (Fluxo OCR) no TrueNAS SCALE através do script de rollout automatizado.

---

## Estratégia de Implantação (Rollout)

O **microservices-ocr** é executado como um aplicativo TrueNAS SCALE (`ix-chart`) sob o nome de release `ocr-recognizer` no namespace `ix-ocr-recognizer`.

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
| `OCR_TRUENAS_DEPLOYMENTS`| Lista de deployments a validar, separados por vírgula | `ocr-recognizer` |
| `OCR_TRUENAS_DATASET` | Dataset ZFS a ser verificado no preflight | `NVME/ocr-apps/data` |
| `OCR_DATA_DIR` | Caminho no host correspondente ao dataset persistente | `/mnt/NVME/ocr-apps/data` |
| `OCR_ROLLOUT_HTTP_ATTEMPTS`| Número de tentativas de checagem HTTP | `12` |
| `OCR_ROLLOUT_HTTP_RETRY_DELAY`| Intervalo em segundos entre tentativas de checagem HTTP | `5` |
| `OCR_IMAGE_PULL_POLICY` | Política de pull de imagem do Kubernetes | `IfNotPresent` |

---

## Como Utilizar o Script de Rollout

O script deve ser executado no próprio shell do TrueNAS (ou ambiente com acesso ao comando `midclt` e `k3s`).

### 1. Preflight (Verificação Prévia)
Valida o ambiente e a disponibilidade da nova imagem sem alterar o estado de produção.
```bash
python3 deploy/truenas-apps/scripts/rollout.py preflight --image-tag sha-e3b0c44
```

### 2. Backup Manual
Gera um backup avulso sob demanda dos dados persistentes (SQLite e arquivos de trabalho):
```bash
python3 deploy/truenas-apps/scripts/rollout.py backup
```

### 3. Deploy (Implantação Completa)
Executa a esteira completa: preflight -> backup -> atualização da imagem -> validação HTTP e de prontidão.
```bash
python3 deploy/truenas-apps/scripts/rollout.py deploy --image-tag sha-e3b0c44
```
*Caso necessite ignorar o backup (ex: migrações de teste), passe `--skip-backup --confirm-skip-backup`.*

### 4. Validação Manual
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
