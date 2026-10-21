# Armazenamento e operação da entrega RAG

## Limites persistentes aplicados no TrueNAS em 20/10/2026

| Dataset | Quota |
|---|---:|
| `NVME/ocr-apps` | 20 GiB |
| `NVME/ocr-apps/data` | 8 GiB |
| `NVME/ocr-apps/backups` | 8 GiB |
| `NVME/ocr-apps/releases` | 4 GiB |

O limite do pai prevalece sobre a soma dos filhos. São quotas físicas,
incluindo snapshots e descendentes, sem reserva antecipada de capacidade.
Não são prazos de retenção nem garantia de armazenamento imutável.
Os valores anteriores eram ilimitados. A alteração não apagou arquivos,
não reiniciou o serviço e não mudou sua imagem f77349a.

O único volume persistente observado no deployment `ocr-recognizer-ix-chart`
mapeia `/tmp/ocr-recognizer` para data. A verificação dentro do container
confirmou aproximadamente 8 GiB visíveis e health HTTP200 após a alteração.
Dados OCR, banco/WAL, metadados e pacotes da outbox nesse volume compartilham
o mesmo teto. Backups/releases têm limites próprios e também consomem o teto pai.

Temporários que alguma biblioteca crie fora desse volume e imagens do runtime
de containers não estão cobertos pela quota data. O release deve direcionar
TMPDIR para área privada dentro do volume e qualificar limpeza/recuperação,
ou demonstrar outro limite para esse espaço. Não declarar cobertura global
do host com base apenas nas quotas acima.

## Comportamento do candidato

Uma admissão RAG nova exige margem livre de três vezes o tamanho do pacote,
mais `upload_min_free_mb`. Quando falta espaço, responde HTTP429
`rag_storage_pressure` sem criar entrega/hold. Reenvio da mesma entrega
reutiliza o registro e não duplica armazenamento. Esta medição não substitui
a quota imposta pelo filesystem nem elimina falhas de escrita concorrentes.

O worker registra mudanças de estado dos avisos agregados de prazo próximo,
resolução pendente e pressão de armazenamento. Não inclui documentos, URLs,
tokens ou identidades nos logs. O operador deve coletar esses avisos.

## Resolução e preservação

1. Consultar o estado e eventual recibo autenticado da entrega.
2. Recuperar o pacote e verificar tamanho/SHA-256 quando a entrega permanecer
   pendente. Download não prova persistência externa e não libera hold.
3. Confirmar armazenamento no repositório externo antes de abandonar a entrega.
   Cancelamento exige proprietário e artifacts:delete; permite a limpeza
   dos artefatos cujo prazo normal já venceu e não desfaz uma cópia remota.
4. Não resolver falta de espaço apagando banco, snapshots ou arquivos retidos
   por hold. Ausência de recibo não significa ausência de entrega remota.

Continuam pendentes a qualificação operacional desses procedimentos, os valores
da política de entrega para produção e o arquivamento/expiração de recibos.
Somente entregas ativas contam no limite de entradas; recibos concluídos
não bloqueiam novas admissões.

## Evidências e reversão

Na pasta local `planejamento/release-2026-10-20`, os arquivos
`rag-storage-inspect`, `rag-storage-runtime-inspect`, `rag-storage-quota-apply`
e `rag-storage-quota-verify` conservam solicitações e resultados JSON.
Restaurar quota ilimitada é possível, mas exige verificar primeiro se os
valores atuais ainda são exatamente os registrados; nunca sobrescrever uma
política posterior. Nenhuma reversão elimina arquivos ou snapshots.
