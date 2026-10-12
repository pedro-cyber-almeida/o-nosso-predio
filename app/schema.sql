-- Esquema das fases 1 a 3 e entregas 1 a 3: configuração, frações, regras de quotas,
-- cobranças, pagamentos, afetações, anulações, utilizadores, associações,
-- despesas e pagamentos de despesas.
-- Valores monetários em cêntimos inteiros; permilagem em centésimos de permilagem.
-- Datas em formato ISO (YYYY-MM-DD); apresentadas em dia/mês/ano.
-- Períodos de cobrança em 'YYYY-MM' (um mês).
--
-- Convenção dos períodos de responsável (owners):
--   valid_from  = primeiro dia em que a pessoa esteve no papel (inclusivo).
--   valid_to    = último dia em que a pessoa esteve no papel (inclusivo);
--                 NULL significa "em aberto", sem data de fim confirmada.
--   Um novo período do mesmo papel principal tem de começar no dia seguinte
--   ao valid_to do anterior; começar no próprio dia de fim sobrepõe-se.
--   Um período encerrado nunca é apagado: só ganha valid_to.

-- `PRAGMA foreign_keys` é por ligação e é ativado em `db.get_db()`: não pertence
-- ao esquema e dentro de uma transação seria ignorado pelo SQLite.

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fractions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    permilagem INTEGER NOT NULL CHECK (permilagem > 0 AND permilagem <= 100000),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Histórico de responsáveis. Sem ON DELETE CASCADE: apagar uma fração
-- com histórico é recusado pela base de dados.
CREATE TABLE IF NOT EXISTS owners (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fraction_id INTEGER NOT NULL REFERENCES fractions (id),
    name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'proprietario'
        CHECK (role IN ('proprietario', 'coproprietario', 'administrador',
                        'representante', 'contacto')),
    email TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    address TEXT NOT NULL DEFAULT '',
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    source TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    CHECK (valid_to IS NULL OR valid_to >= valid_from)
);

CREATE INDEX IF NOT EXISTS idx_owners_fraction ON owners (fraction_id);

-- Rasto de cada alteração de permilagem. Não existe redistribuição silenciosa:
-- uma fração com alterações registadas deixa de poder ser eliminada, porque o
-- histórico passaria a apontar para uma fração que já não existe.
CREATE TABLE IF NOT EXISTS permilagem_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fraction_id INTEGER NOT NULL REFERENCES fractions (id),
    permilagem_anterior INTEGER NOT NULL CHECK (permilagem_anterior > 0),
    permilagem_nova INTEGER NOT NULL CHECK (permilagem_nova > 0 AND permilagem_nova <= 100000),
    motivo TEXT NOT NULL DEFAULT '',
    alterado_em TEXT NOT NULL,
    CHECK (permilagem_anterior <> permilagem_nova)
);

CREATE INDEX IF NOT EXISTS idx_permilagem_history_fraction
    ON permilagem_history (fraction_id);

-- ---------------------------------------------------------------------------
-- Fase 2: regras de quotas e cobranças mensais.
--
-- Convenção dos períodos de regra (charge_rules), igual à dos responsáveis:
--   valid_from = primeiro dia da vigência (inclusivo), sempre o dia 1 de um mês.
--   valid_to   = último dia da vigência (inclusivo), sempre o último dia de um mês;
--                NULL significa "em aberto", sem data de fim confirmada.
-- Os CHECK garantem na base o que a aplicação também valida: uma regra começa
-- no dia 1 e termina no último dia do mês, por isso a cobertura de meses é
-- sempre exata e um período parcial como 15/01 não pode entrar.
-- ---------------------------------------------------------------------------

-- ---------------------------------------------------------------------------
-- Entrega 2: quota extraordinária.
--
-- É uma decisão pontual, não uma regra que se repete: um montante total, uma
-- forma de o repartir pelas frações abrangidas e um calendário de prestações.
-- Cada fração abrangida fica com uma regra própria em `charge_rules`, que guarda
-- a quota-parte dessa fração; as cobranças emitidas vêm dessas regras e de cada
-- prestação. Assim a consulta, o portal e as afetações de pagamentos continuam a
-- trabalhar com `charges`, como sempre.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS extraordinary_quotas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    descricao TEXT NOT NULL CHECK (length(trim(descricao)) > 0),
    finalidade TEXT NOT NULL DEFAULT '',
    total_cents INTEGER NOT NULL CHECK (total_cents > 0),
    -- Como o total foi dividido pelas frações abrangidas.
    reparticao TEXT NOT NULL CHECK (reparticao IN ('igual', 'permilagem', 'manual')),
    -- Marca do formulário que criou esta quota: um duplo clique ou um reenvio
    -- envia a mesma marca, e a segunda tentativa é recusada em vez de criar uma
    -- segunda igual.
    form_token TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quota_installments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    quota_id INTEGER NOT NULL REFERENCES extraordinary_quotas (id),
    numero INTEGER NOT NULL CHECK (numero >= 1),
    due_date TEXT NOT NULL CHECK (date(due_date) IS NOT NULL),
    UNIQUE (quota_id, numero)
);

CREATE TABLE IF NOT EXISTS charge_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fraction_id INTEGER NOT NULL REFERENCES fractions (id),
    rubric TEXT NOT NULL CHECK (
        rubric IN ('quota_ordinaria', 'reforco_fundo', 'quota_extraordinaria')
    ),
    -- Regra de uma quota extraordinária: diz de que decisão veio. Regra recorrente
    -- (ordinária e reforço) não pertence a nenhuma quota.
    quota_id INTEGER REFERENCES extraordinary_quotas (id),
    -- Texto que a cobrança há de mostrar. Guardado aqui e copiado para a cobrança
    -- no momento da emissão: editar a regra não reescreve o que já foi emitido.
    descricao TEXT NOT NULL DEFAULT '',
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    source TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    -- Marca do formulário que autorizou a última mudança de valor desta regra.
    edit_token TEXT,
    CHECK (
        date(valid_from) IS NOT NULL
        AND strftime('%d', valid_from) = '01'
        AND (
            valid_to IS NULL
            OR (
                date(valid_to) IS NOT NULL
                AND strftime('%d', date(valid_to, '+1 day')) = '01'
                AND valid_to >= valid_from
            )
        )
    ),
    -- Uma regra extraordinária pertence sempre a uma quota e tem fim definido:
    -- nunca fica "em aberto", porque não é algo que se repita todos os meses.
    CHECK (
        (rubric = 'quota_extraordinaria' AND quota_id IS NOT NULL AND valid_to IS NOT NULL)
        OR (rubric <> 'quota_extraordinaria' AND quota_id IS NULL)
    )
);

-- Uma regra em aberto por fração e rubrica: duas regras sem fim para a mesma
-- fração deixariam a cobrança do mês sem saber qual aplicar.
CREATE UNIQUE INDEX IF NOT EXISTS idx_charge_rules_aberta
    ON charge_rules (fraction_id, rubric) WHERE valid_to IS NULL;

-- Sobreposições não expressáveis num índice único ficam travadas aqui, na base,
-- para que nenhum caminho (nem uma escrita feita à mão) crie duas regras a valer
-- no mesmo mês. A aplicação valida antes, mas a base é a última barreira.
-- Só travam as rubricas que se repetem: duas quotas extraordinárias diferentes no
-- mesmo mês são legítimas (duas decisões distintas) e têm de poder coexistir.
CREATE TRIGGER IF NOT EXISTS charge_rules_sem_sobreposicao_insert
BEFORE INSERT ON charge_rules
BEGIN
    SELECT RAISE(ABORT, 'regra de quota sobreposta: já existe uma regra para esta fração e rubrica no mesmo período')
    WHERE NEW.rubric <> 'quota_extraordinaria'
      AND EXISTS (
        SELECT 1 FROM charge_rules AS existente
        WHERE existente.fraction_id = NEW.fraction_id
          AND existente.rubric = NEW.rubric
          AND existente.valid_from <= COALESCE(NEW.valid_to, '9999-12-31')
          AND COALESCE(existente.valid_to, '9999-12-31') >= NEW.valid_from
      );
END;

CREATE TRIGGER IF NOT EXISTS charge_rules_sem_sobreposicao_update
BEFORE UPDATE OF valid_from, valid_to ON charge_rules
BEGIN
    SELECT RAISE(ABORT, 'regra de quota sobreposta: já existe uma regra para esta fração e rubrica no mesmo período')
    WHERE NEW.rubric <> 'quota_extraordinaria'
      AND EXISTS (
        SELECT 1 FROM charge_rules AS existente
        WHERE existente.id <> OLD.id
          AND existente.fraction_id = NEW.fraction_id
          AND existente.rubric = NEW.rubric
          AND existente.valid_from <= COALESCE(NEW.valid_to, '9999-12-31')
          AND COALESCE(existente.valid_to, '9999-12-31') >= NEW.valid_from
      );
END;

-- Cobrança emitida: um valor por cobrar, não dinheiro recebido.
--
-- A chave de unicidade mudou na entrega 2. Antes era (fraction_id, rubric, period),
-- o que bastava enquanto só havia rubricas recorrentes: gerar o mesmo mês outra vez
-- não criava uma segunda linha. Com quotas extraordinárias, duas decisões diferentes
-- no mesmo mês têm de poder existir para a mesma fração. A unicidade passa a incluir
-- a regra de que a cobrança veio, que é o que a identifica:
--   * a mesma quota extraordinária emitida duas vezes (duplo clique, reenvio) é
--     recusada, porque a regra e o mês são os mesmos;
--   * duas quotas extraordinárias diferentes no mesmo mês são aceites, porque as
--     regras são diferentes.
-- Para as rubricas recorrentes a garantia antiga mantém-se, como índice parcial.
CREATE TABLE IF NOT EXISTS charges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fraction_id INTEGER NOT NULL REFERENCES fractions (id),
    rule_id INTEGER NOT NULL REFERENCES charge_rules (id),
    rubric TEXT NOT NULL CHECK (
        rubric IN ('quota_ordinaria', 'reforco_fundo', 'quota_extraordinaria')
    ),
    period TEXT NOT NULL CHECK (
        period GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]'
        AND substr(period, 6, 2) BETWEEN '01' AND '12'
    ),
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    due_date TEXT,
    generated_at TEXT NOT NULL,
    -- Descrição e vencimento que a cobrança tinha quando foi emitida.
    descricao TEXT NOT NULL DEFAULT '',
    -- Marca do formulário que autorizou a última correção desta cobrança.
    edit_token TEXT,
    UNIQUE (fraction_id, rubric, period, rule_id)
);

CREATE INDEX IF NOT EXISTS idx_charges_period ON charges (period);
CREATE INDEX IF NOT EXISTS idx_charges_fraction ON charges (fraction_id);
CREATE INDEX IF NOT EXISTS idx_charges_rule ON charges (rule_id);

-- Uma só cobrança ordinária (ou de reforço) por fração e mês, como sempre.
CREATE UNIQUE INDEX IF NOT EXISTS idx_charges_recorrentes_unicas
    ON charges (fraction_id, rubric, period)
    WHERE rubric IN ('quota_ordinaria', 'reforco_fundo');

-- O histórico financeiro não se apaga. Corrigir uma cobrança emitida deixou de ser
-- proibido em absoluto e passou a ser uma ação justificada: só passa um UPDATE se
-- existir a revisão que o autoriza, escrita na mesma transação, com motivo.
CREATE TRIGGER IF NOT EXISTS cobrancas_so_se_corrigem_com_motivo
BEFORE UPDATE ON charges
BEGIN
    SELECT RAISE(ABORT, 'cobrança só se corrige com motivo registado: abre a correção na aplicação')
    WHERE NOT EXISTS (
        SELECT 1 FROM charge_revisions
        WHERE charge_id = OLD.id AND edit_token = NEW.edit_token
    );
END;

-- A correção toca no valor e no vencimento. O que diz de onde a cobrança veio
-- (fração, rubrica, mês, regra, momento da emissão) não se mexe: mudar isso seria
-- outra cobrança, e isso é emitir uma nova, não corrigir esta.
CREATE TRIGGER IF NOT EXISTS cobrancas_sem_apagar_a_origem
BEFORE UPDATE ON charges
BEGIN
    SELECT RAISE(ABORT, 'a origem de uma cobrança emitida não se altera: fração, rubrica, mês e regra conservam-se')
    WHERE OLD.fraction_id IS NOT NEW.fraction_id
       OR OLD.rubric IS NOT NEW.rubric
       OR OLD.period IS NOT NEW.period
       OR OLD.rule_id IS NOT NEW.rule_id
       OR OLD.generated_at IS NOT NEW.generated_at;
END;

-- Uma correção não pode deixar a cobrança abaixo do que já foi pago e afetado a ela:
-- isso apagaria dinheiro registado. A aplicação recusa antes e explica o que ajustar;
-- este travão é a última barreira.
CREATE TRIGGER IF NOT EXISTS cobrancas_nao_ficam_abaixo_do_pago
BEFORE UPDATE OF amount_cents ON charges
BEGIN
    SELECT RAISE(ABORT, 'a cobrança não pode ficar abaixo do que já foi pago e afetado a ela')
    WHERE NEW.amount_cents < (
        SELECT COALESCE(SUM(a.amount_cents), 0)
        FROM payment_allocations AS a
        JOIN payments AS p ON p.id = a.payment_id
        WHERE a.charge_id = OLD.id AND p.cancelled_at IS NULL
    );
END;

CREATE TRIGGER IF NOT EXISTS charges_sem_delete
BEFORE DELETE ON charges
BEGIN
    SELECT RAISE(ABORT, 'cobranças emitidas não podem ser apagadas: o histórico financeiro conserva-se');
END;

-- Correção de uma cobrança emitida: fica o antes e o depois, a data, quem fez e o
-- motivo. Nunca se apaga a cobrança nem se anula um pagamento para a deixar passar.
CREATE TABLE IF NOT EXISTS charge_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    charge_id INTEGER NOT NULL REFERENCES charges (id),
    changed_at TEXT NOT NULL,
    motivo TEXT NOT NULL CHECK (length(trim(motivo)) > 0),
    changed_by TEXT NOT NULL DEFAULT '',
    -- Marca do formulário que originou esta correção: um reenvio não cria duas.
    edit_token TEXT NOT NULL UNIQUE,
    amount_before INTEGER NOT NULL CHECK (amount_before > 0),
    amount_after INTEGER NOT NULL CHECK (amount_after > 0),
    due_before TEXT,
    due_after TEXT,
    description_before TEXT NOT NULL DEFAULT '',
    description_after TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_charge_revisions_charge ON charge_revisions (charge_id);

CREATE TRIGGER IF NOT EXISTS correcoes_de_cobranca_sem_update
BEFORE UPDATE ON charge_revisions
BEGIN
    SELECT RAISE(ABORT, 'o registo de uma correção de cobrança não se altera');
END;

CREATE TRIGGER IF NOT EXISTS correcoes_de_cobranca_sem_delete
BEFORE DELETE ON charge_revisions
BEGIN
    SELECT RAISE(ABORT, 'o registo de uma correção de cobrança não se apaga');
END;

-- ---------------------------------------------------------------------------
-- Mudar o valor de uma regra afeta só o que ainda há de ser emitido: as cobranças
-- já emitidas guardam o próprio valor e não são tocadas. Ainda assim a mudança fica
-- registada com o motivo, para que se saiba por que razão o prédio passou a pagar
-- outro valor a partir de certa data.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS charge_rule_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id INTEGER NOT NULL REFERENCES charge_rules (id),
    changed_at TEXT NOT NULL,
    motivo TEXT NOT NULL CHECK (length(trim(motivo)) > 0),
    changed_by TEXT NOT NULL DEFAULT '',
    edit_token TEXT NOT NULL UNIQUE,
    amount_before INTEGER NOT NULL CHECK (amount_before > 0),
    amount_after INTEGER NOT NULL CHECK (amount_after > 0),
    valid_to_before TEXT,
    valid_to_after TEXT
);

CREATE INDEX IF NOT EXISTS idx_charge_rule_revisions_rule ON charge_rule_revisions (rule_id);

CREATE TRIGGER IF NOT EXISTS regras_sem_mudar_valor_sem_motivo
BEFORE UPDATE OF amount_cents ON charge_rules
BEGIN
    SELECT RAISE(ABORT, 'o valor de uma regra só muda com motivo registado')
    WHERE NEW.amount_cents <> OLD.amount_cents
      AND NOT EXISTS (
        SELECT 1 FROM charge_rule_revisions
        WHERE rule_id = OLD.id AND edit_token = NEW.edit_token
      );
END;

CREATE TRIGGER IF NOT EXISTS correcoes_de_regra_sem_update
BEFORE UPDATE ON charge_rule_revisions
BEGIN
    SELECT RAISE(ABORT, 'o registo de uma mudança de regra não se altera');
END;

CREATE TRIGGER IF NOT EXISTS correcoes_de_regra_sem_delete
BEFORE DELETE ON charge_rule_revisions
BEGIN
    SELECT RAISE(ABORT, 'o registo de uma mudança de regra não se apaga');
END;

-- Uma quota extraordinária que já originou cobranças não é alterada nem apagada:
-- as cobranças que dela vieram conservam valor, vencimento e descrição. Antes de
-- emitir, corrigir a decisão é livre.
CREATE TRIGGER IF NOT EXISTS quotas_extraordinarias_sem_mudar_a_emitida
BEFORE UPDATE ON extraordinary_quotas
BEGIN
    SELECT RAISE(ABORT, 'quota extraordinária com cobranças emitidas não é alterada: corrige as cobranças uma a uma')
    WHERE EXISTS (
        SELECT 1 FROM charges AS c
        JOIN charge_rules AS r ON r.id = c.rule_id
        WHERE r.quota_id = OLD.id
    );
END;

CREATE TRIGGER IF NOT EXISTS quotas_extraordinarias_sem_apagar_a_emitida
BEFORE DELETE ON extraordinary_quotas
BEGIN
    SELECT RAISE(ABORT, 'quota extraordinária com cobranças emitidas não é apagada: o histórico financeiro conserva-se')
    WHERE EXISTS (
        SELECT 1 FROM charges AS c
        JOIN charge_rules AS r ON r.id = c.rule_id
        WHERE r.quota_id = OLD.id
    );
END;

CREATE TRIGGER IF NOT EXISTS prestacoes_sem_mudar_as_de_quota_emitida
BEFORE UPDATE ON quota_installments
BEGIN
    SELECT RAISE(ABORT, 'as prestações de uma quota extraordinária já emitida não são alteradas')
    WHERE EXISTS (
        SELECT 1 FROM charges AS c
        JOIN charge_rules AS r ON r.id = c.rule_id
        WHERE r.quota_id = OLD.quota_id
    );
END;

CREATE TRIGGER IF NOT EXISTS prestacoes_sem_apagar_as_de_quota_emitida
BEFORE DELETE ON quota_installments
BEGIN
    SELECT RAISE(ABORT, 'as prestações de uma quota extraordinária já emitida não são apagadas')
    WHERE EXISTS (
        SELECT 1 FROM charges AS c
        JOIN charge_rules AS r ON r.id = c.rule_id
        WHERE r.quota_id = OLD.quota_id
    );
END;

-- ---------------------------------------------------------------------------
-- Fase 3: pagamentos, afetações, créditos e anulações.
--
-- Convenção:
--   `payments` é o dinheiro que efetivamente entrou: um registo por movimento.
--   `payment_allocations` é a distribuição desse dinheiro por cobranças concretas.
--   O crédito de uma fração é o que foi recebido e ainda não foi afetado:
--     crédito = pagamentos válidos - afetações dos pagamentos válidos.
--   Não é uma coluna nem uma tabela: calcular evita contar o mesmo dinheiro
--   duas vezes, que é exatamente o que "afetar um crédito" não pode fazer.
--   "Válido" significa `cancelled_at IS NULL`. Anular não apaga nem altera:
--   marca o pagamento, e todas as somas da aplicação deixam de o contar. As
--   afetações ficam como estavam, por isso a anulação reabre os montantes das
--   cobranças que aquele pagamento tinha liquidado.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fraction_id INTEGER NOT NULL REFERENCES fractions (id),
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    paid_on TEXT NOT NULL CHECK (date(paid_on) IS NOT NULL),
    method TEXT NOT NULL DEFAULT 'outro'
        CHECK (method IN ('transferencia', 'dinheiro', 'cheque', 'debito_direto', 'outro')),
    reference TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    cancelled_at TEXT,
    cancellation_reason TEXT,
    -- Marca do formulário que originou o registo. Um duplo clique envia o mesmo
    -- formulário duas vezes: o índice único não deixa entrar a segunda cópia.
    form_token TEXT,
    -- Data e motivo da última correção. Preencher estas duas colunas é o que
    -- autoriza a alteração: ver o gatilho `pagamentos_so_se_corrigem_com_motivo`.
    edited_at TEXT,
    edit_reason TEXT,
    CHECK (
        (cancelled_at IS NULL AND cancellation_reason IS NULL)
        OR (cancelled_at IS NOT NULL AND cancellation_reason <> '')
    ),
    CHECK (
        (edited_at IS NULL AND edit_reason IS NULL)
        OR (edited_at IS NOT NULL AND edit_reason <> '')
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_form_token
    ON payments (form_token) WHERE form_token IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_payments_fraction ON payments (fraction_id);
CREATE INDEX IF NOT EXISTS idx_payments_period ON payments (paid_on);

CREATE TABLE IF NOT EXISTS payment_allocations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    payment_id INTEGER NOT NULL REFERENCES payments (id),
    charge_id INTEGER NOT NULL REFERENCES charges (id),
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    created_at TEXT NOT NULL,
    -- Um pagamento não afeta duas vezes a mesma cobrança: repetir seria duplicar.
    UNIQUE (payment_id, charge_id)
);

CREATE INDEX IF NOT EXISTS idx_allocations_payment ON payment_allocations (payment_id);
CREATE INDEX IF NOT EXISTS idx_allocations_charge ON payment_allocations (charge_id);

-- Uma afetação só pode liquidar uma cobrança da mesma fração do pagamento.
CREATE TRIGGER IF NOT EXISTS afecoes_da_mesma_fracao
BEFORE INSERT ON payment_allocations
BEGIN
    SELECT RAISE(ABORT, 'afetação inválida: um pagamento só liquidou cobranças da sua fração')
    WHERE (SELECT fraction_id FROM payments WHERE id = NEW.payment_id)
       <> (SELECT fraction_id FROM charges WHERE id = NEW.charge_id);
END;

CREATE TRIGGER IF NOT EXISTS afecoes_da_mesma_fracao_update
BEFORE UPDATE ON payment_allocations
BEGIN
    SELECT RAISE(ABORT, 'afetação inválida: um pagamento só liquidou cobranças da sua fração')
    WHERE (SELECT fraction_id FROM payments WHERE id = NEW.payment_id)
       <> (SELECT fraction_id FROM charges WHERE id = NEW.charge_id);
END;

-- Uma afetação não muda de cobrança nem de pagamento: retirar e acrescentar outra.
CREATE TRIGGER IF NOT EXISTS afecoes_nao_mudam_de_cobranca
BEFORE UPDATE ON payment_allocations
BEGIN
    SELECT RAISE(ABORT, 'uma afetação não muda de cobrança: retira-a e acrescenta outra')
    WHERE OLD.payment_id IS NOT NEW.payment_id
       OR OLD.charge_id IS NOT NEW.charge_id;
END;

-- Um pagamento anulado não volta a receber afetações: o dinheiro saiu do registo.
CREATE TRIGGER IF NOT EXISTS afecoes_sem_pagamento_anulado
BEFORE INSERT ON payment_allocations
BEGIN
    SELECT RAISE(ABORT, 'afetação inválida: o pagamento está anulado')
    WHERE (SELECT cancelled_at FROM payments WHERE id = NEW.payment_id) IS NOT NULL;
END;

CREATE TRIGGER IF NOT EXISTS afecoes_sem_pagamento_anulado_update
BEFORE UPDATE ON payment_allocations
BEGIN
    SELECT RAISE(ABORT, 'afetação inválida: o pagamento está anulado')
    WHERE (SELECT cancelled_at FROM payments WHERE id = NEW.payment_id) IS NOT NULL;
END;

-- As afetações não excedem o pagamento que as originou.
CREATE TRIGGER IF NOT EXISTS afecoes_nao_excedem_pagamento
AFTER INSERT ON payment_allocations
BEGIN
    SELECT RAISE(ABORT, 'afetação inválida: a soma das afetações excede o pagamento')
    WHERE (SELECT amount_cents FROM payments WHERE id = NEW.payment_id)
        < (SELECT COALESCE(SUM(amount_cents), 0)
           FROM payment_allocations
           WHERE payment_id = NEW.payment_id);
END;

CREATE TRIGGER IF NOT EXISTS afecoes_nao_excedem_pagamento_update
AFTER UPDATE OF amount_cents ON payment_allocations
BEGIN
    SELECT RAISE(ABORT, 'afetação inválida: a soma das afetações excede o pagamento')
    WHERE (SELECT amount_cents FROM payments WHERE id = NEW.payment_id)
        < (SELECT COALESCE(SUM(amount_cents), 0)
           FROM payment_allocations
           WHERE payment_id = NEW.payment_id);
END;

-- Uma afetação não excede o que falta pagar nessa cobrança. Só contam os pagamentos
-- válidos: os anulados deixaram de liquidar seja o que for.
CREATE TRIGGER IF NOT EXISTS afecoes_nao_excedem_cobranca
AFTER INSERT ON payment_allocations
BEGIN
    SELECT RAISE(ABORT, 'afetação inválida: excede o montante em dívida dessa cobrança')
    WHERE (SELECT amount_cents FROM charges WHERE id = NEW.charge_id)
        < (SELECT COALESCE(SUM(a.amount_cents), 0)
           FROM payment_allocations AS a
           JOIN payments AS p ON p.id = a.payment_id
           WHERE a.charge_id = NEW.charge_id
             AND p.cancelled_at IS NULL);
END;

CREATE TRIGGER IF NOT EXISTS afecoes_nao_excedem_cobranca_update
AFTER UPDATE OF amount_cents ON payment_allocations
BEGIN
    SELECT RAISE(ABORT, 'afetação inválida: excede o montante em dívida dessa cobrança')
    WHERE (SELECT amount_cents FROM charges WHERE id = NEW.charge_id)
        < (SELECT COALESCE(SUM(a.amount_cents), 0)
           FROM payment_allocations AS a
           JOIN payments AS p ON p.id = a.payment_id
           WHERE a.charge_id = NEW.charge_id
             AND p.cancelled_at IS NULL);
END;

-- As afetações podem ser retiradas e repostas quando o pagamento é corrigido, porque
-- redistribuir faz parte da edição. O que se conserva é o valor distribuído em cada
-- correção, guardado em `payment_allocation_revisions` pela aplicação, dentro da
-- mesma transação. Os travões acima continuam a valer em qualquer escrita.

-- ---------------------------------------------------------------------------
-- Rasto de correções de um pagamento.
--
-- O pagamento é um registo só: as somas da aplicação lêem sempre a linha atual,
-- por isso corrigir não duplica receita. O que muda fica guardado aqui, com os
-- valores anteriores e novos, a data e o motivo.
-- `changed_by` guarda o nome de utilizador de quem corrigiu. Continua vazio
-- quando a correção vem de uma escrita direta à base: a identidade não é inventada.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS payment_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    payment_id INTEGER NOT NULL REFERENCES payments (id),
    changed_at TEXT NOT NULL,
    reason TEXT NOT NULL CHECK (reason <> ''),
    changed_by TEXT NOT NULL DEFAULT '',
    -- Marca do formulário que originou esta correção: um reenvio não cria duas.
    edit_token TEXT NOT NULL UNIQUE,
    old_fraction_id INTEGER NOT NULL REFERENCES fractions (id),
    old_amount_cents INTEGER NOT NULL CHECK (old_amount_cents > 0),
    old_paid_on TEXT NOT NULL,
    old_method TEXT NOT NULL,
    old_reference TEXT NOT NULL DEFAULT '',
    old_source TEXT NOT NULL DEFAULT '',
    new_fraction_id INTEGER NOT NULL REFERENCES fractions (id),
    new_amount_cents INTEGER NOT NULL CHECK (new_amount_cents > 0),
    new_paid_on TEXT NOT NULL,
    new_method TEXT NOT NULL,
    new_reference TEXT NOT NULL DEFAULT '',
    new_source TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_payment_revisions_payment ON payment_revisions (payment_id);

CREATE TABLE IF NOT EXISTS payment_allocation_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    revision_id INTEGER NOT NULL REFERENCES payment_revisions (id),
    lado TEXT NOT NULL CHECK (lado IN ('antes', 'depois')),
    charge_id INTEGER NOT NULL REFERENCES charges (id),
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    UNIQUE (revision_id, lado, charge_id)
);

CREATE TRIGGER IF NOT EXISTS correcoes_sem_update
BEFORE UPDATE ON payment_revisions
BEGIN
    SELECT RAISE(ABORT, 'o rasto de correções não é alterado');
END;

CREATE TRIGGER IF NOT EXISTS correcoes_sem_delete
BEFORE DELETE ON payment_revisions
BEGIN
    SELECT RAISE(ABORT, 'o rasto de correções não é apagado');
END;

CREATE TRIGGER IF NOT EXISTS correcoes_de_afecoes_sem_update
BEFORE UPDATE ON payment_allocation_revisions
BEGIN
    SELECT RAISE(ABORT, 'o rasto de correções não é alterado');
END;

CREATE TRIGGER IF NOT EXISTS correcoes_de_afecoes_sem_delete
BEFORE DELETE ON payment_allocation_revisions
BEGIN
    SELECT RAISE(ABORT, 'o rasto de correções não é apagado');
END;

-- ---------------------------------------------------------------------------
-- O que continua proibido num pagamento.
--
-- A imutabilidade absoluta da fase 3 deu lugar a correção justificada: os campos
-- de negócio só mudam se a mesma declaração preencher `edited_at` e `edit_reason`.
-- É o travão que impede a alteração silenciosa do histórico.
-- ---------------------------------------------------------------------------

CREATE TRIGGER IF NOT EXISTS pagamentos_so_se_corrigem_com_motivo
BEFORE UPDATE ON payments
BEGIN
    SELECT RAISE(ABORT, 'um pagamento só é alterado por uma correção com motivo registado')
    WHERE (OLD.amount_cents IS NOT NEW.amount_cents
        OR OLD.paid_on IS NOT NEW.paid_on
        OR OLD.fraction_id IS NOT NEW.fraction_id
        OR OLD.method IS NOT NEW.method
        OR OLD.reference IS NOT NEW.reference
        OR OLD.source IS NOT NEW.source)
      AND (NEW.edited_at IS NULL OR NEW.edit_reason IS NULL OR NEW.edit_reason = '');
END;

-- Identificadores internos e rasto de auditoria não são campos de negócio.
-- O motivo da anulação é escrito no momento em que a anulação acontece; depois disso
-- também não se altera.
CREATE TRIGGER IF NOT EXISTS pagamentos_sem_alterar_auditoria
BEFORE UPDATE ON payments
BEGIN
    SELECT RAISE(ABORT, 'identificadores e rasto de auditoria não são campos editáveis')
    WHERE OLD.id IS NOT NEW.id
       OR OLD.created_at IS NOT NEW.created_at
       OR OLD.form_token IS NOT NEW.form_token
       OR (OLD.cancellation_reason IS NOT NULL
           AND OLD.cancellation_reason IS NOT NEW.cancellation_reason);
END;

-- Um pagamento anulado não é editado nem reativado: uma anulação não se repete e
-- os valores de um registo anulado ficam congelados.
CREATE TRIGGER IF NOT EXISTS pagamentos_sem_editar_anulado
BEFORE UPDATE ON payments
BEGIN
    SELECT RAISE(ABORT, 'um pagamento anulado não é editado nem reativado')
    WHERE OLD.cancelled_at IS NOT NULL;
END;

CREATE TRIGGER IF NOT EXISTS pagamentos_sem_delete
BEFORE DELETE ON payments
BEGIN
    SELECT RAISE(ABORT, 'um pagamento não é apagado: anula-o e o histórico conserva-se');
END;

-- Baixar o valor de um pagamento não pode deixar afetações acima dele.
CREATE TRIGGER IF NOT EXISTS pagamentos_nao_ficam_com_afecoes_a_mais
AFTER UPDATE OF amount_cents ON payments
BEGIN
    SELECT RAISE(ABORT, 'um pagamento não pode ficar com afetações acima do seu valor')
    WHERE (SELECT COALESCE(SUM(amount_cents), 0)
           FROM payment_allocations
           WHERE payment_id = NEW.id) > NEW.amount_cents;
END;

-- ---------------------------------------------------------------------------
-- Entrega 1: utilizadores, perfis e associações a frações.
--
-- Dois papéis: `administrador` e `condomino`. Sem registo público: o primeiro
-- administrador é criado por um percurso protegido, sem palavra-passe predefinida.
-- Um utilizador pode estar associado a várias frações e uma fração pode ter
-- vários utilizadores. A associação é administrativa: não é movimento financeiro.
-- Um utilizador não é apagado: é desativado, para que o rasto de correções
-- (`payment_revisions.changed_by`) continue a apontar para uma pessoa real.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL CHECK (password_hash <> ''),
    display_name TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL CHECK (role IN ('administrador', 'condomino')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    password_changed_at TEXT,
    CHECK (username <> '')
);

CREATE TABLE IF NOT EXISTS user_fractions (
    user_id INTEGER NOT NULL REFERENCES users (id),
    fraction_id INTEGER NOT NULL REFERENCES fractions (id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, fraction_id)
);

CREATE INDEX IF NOT EXISTS idx_user_fractions_fraction ON user_fractions (fraction_id);

-- O nome de utilizador é a identidade nos registos: não muda.
CREATE TRIGGER IF NOT EXISTS utilizadores_sem_mudar_nome
BEFORE UPDATE OF username ON users
BEGIN
    SELECT RAISE(ABORT, 'o nome de utilizador não muda: desativa o registo e cria outro');
END;

CREATE TRIGGER IF NOT EXISTS utilizadores_sem_delete
BEFORE DELETE ON users
BEGIN
    SELECT RAISE(ABORT, 'um utilizador não é apagado: desativa-o e o rasto de correções conserva-se');
END;

-- ---------------------------------------------------------------------------
-- Entrega 3: despesas e pagamentos de despesas.
--
-- Convenção, igual à dos pagamentos de condóminos:
--   `expenses` é a obrigação registada: uma despesa não é dinheiro que saiu.
--   `expense_payments` é o dinheiro que efetivamente saiu: um registo por movimento.
--   O valor em falta de uma despesa é calculado (valor - pagamentos válidos),
--   nunca guardado: uma coluna de "pago" seria mais um sítio onde o mesmo
--   dinheiro podia ser contado duas vezes.
--   "Válido" significa `cancelled_at IS NULL`. Anular não apaga nem altera:
--   marca o pagamento, e todas as somas deixam de o contar — o valor em falta
--   reabre. Uma anulação não é uma devolução bancária: isso seria um movimento
--   novo, com a sua própria data e valor.
--   As categorias são definidas pelo administrador; não há lista rígida no código.
--   `referencia` é a referência documental (fatura, recibo): texto livre, preparado
--   para a ligação a documentos das entregas seguintes.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS expense_categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nome TEXT NOT NULL UNIQUE CHECK (length(trim(nome)) > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Uma categoria em uso não é renomeada nem apagada: as despesas que a usam
-- conservam o rótulo com que foram registadas. Para mudar um rótulo, cria-se
-- outra categoria e reafetam-se as despesas na edição justificada de cada uma.
CREATE TRIGGER IF NOT EXISTS categorias_sem_apagar_as_usadas
BEFORE DELETE ON expense_categories
WHEN EXISTS (SELECT 1 FROM expenses WHERE category_id = OLD.id)
BEGIN
    SELECT RAISE(ABORT, 'categoria em uso por despesas registadas não é apagada: reafeta as despesas e depois apaga-a');
END;

CREATE TRIGGER IF NOT EXISTS categorias_sem_mudar_as_usadas
BEFORE UPDATE OF nome ON expense_categories
WHEN EXISTS (SELECT 1 FROM expenses WHERE category_id = OLD.id)
BEGIN
    SELECT RAISE(ABORT, 'categoria em uso por despesas registadas não é renomeada: cria a categoria certa e reafeta as despesas');
END;

CREATE TABLE IF NOT EXISTS expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    descricao TEXT NOT NULL CHECK (length(trim(descricao)) > 0),
    fornecedor TEXT NOT NULL DEFAULT '',
    category_id INTEGER NOT NULL REFERENCES expense_categories (id),
    -- Referência documental (nº de fatura, recibo): opcional e livre.
    referencia TEXT NOT NULL DEFAULT '',
    data TEXT NOT NULL CHECK (date(data) IS NOT NULL),
    due_date TEXT CHECK (due_date IS NULL OR date(due_date) IS NOT NULL),
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    -- Que bolsa paga esta despesa: ordinária ou fundo de reserva.
    -- É uma classificação de despesa, não um movimento: não mexe na caixa.
    classificacao TEXT NOT NULL DEFAULT 'ordinario'
        CHECK (classificacao IN ('ordinario', 'fundo_reserva')),
    notas TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    cancelled_at TEXT,
    cancellation_reason TEXT,
    -- Marca do formulário que criou a despesa: duplo clique não cria duas.
    form_token TEXT,
    -- Marca do formulário que autorizou a última correção. Ver o travão abaixo.
    edit_token TEXT,
    CHECK (
        (cancelled_at IS NULL AND cancellation_reason IS NULL)
        OR (cancelled_at IS NOT NULL AND cancellation_reason <> '')
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_expenses_form_token
    ON expenses (form_token) WHERE form_token IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_expenses_category ON expenses (category_id);
CREATE INDEX IF NOT EXISTS idx_expenses_data ON expenses (data);

CREATE TABLE IF NOT EXISTS expense_payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    expense_id INTEGER NOT NULL REFERENCES expenses (id),
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    paid_on TEXT NOT NULL CHECK (date(paid_on) IS NOT NULL),
    method TEXT NOT NULL DEFAULT 'outro'
        CHECK (method IN ('transferencia', 'dinheiro', 'cheque', 'debito_direto', 'outro')),
    reference TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    cancelled_at TEXT,
    cancellation_reason TEXT,
    form_token TEXT,
    edit_token TEXT,
    CHECK (
        (cancelled_at IS NULL AND cancellation_reason IS NULL)
        OR (cancelled_at IS NOT NULL AND cancellation_reason <> '')
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_expense_payments_form_token
    ON expense_payments (form_token) WHERE form_token IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_expense_payments_expense ON expense_payments (expense_id);
CREATE INDEX IF NOT EXISTS idx_expense_payments_period ON expense_payments (paid_on);

-- O valor em falta nunca é guardado: é o que resulta dos pagamentos válidos.
-- Uma despesa só pode ter pagamentos até ao seu valor. Este travão é a última
-- barreira: a aplicação recusa antes e mostra o valor em falta.
CREATE TRIGGER IF NOT EXISTS pagamentos_de_despesa_nao_excedem_despesa
AFTER INSERT ON expense_payments
BEGIN
    SELECT RAISE(ABORT, 'pagamento de despesa inválido: a soma dos pagamentos válidos excede o valor da despesa')
    WHERE (SELECT amount_cents FROM expenses WHERE id = NEW.expense_id)
        < (SELECT COALESCE(SUM(amount_cents), 0)
           FROM expense_payments
           WHERE expense_id = NEW.expense_id AND cancelled_at IS NULL);
END;

CREATE TRIGGER IF NOT EXISTS pagamentos_de_despesa_nao_excedem_despesa_update
AFTER UPDATE OF amount_cents ON expense_payments
BEGIN
    SELECT RAISE(ABORT, 'pagamento de despesa inválido: a soma dos pagamentos válidos excede o valor da despesa')
    WHERE (SELECT amount_cents FROM expenses WHERE id = NEW.expense_id)
        < (SELECT COALESCE(SUM(amount_cents), 0)
           FROM expense_payments
           WHERE expense_id = NEW.expense_id AND cancelled_at IS NULL);
END;

-- Uma despesa anulada não recebe pagamentos: o registo ficou para trás.
CREATE TRIGGER IF NOT EXISTS pagamentos_de_despesa_sem_despesa_anulada
BEFORE INSERT ON expense_payments
BEGIN
    SELECT RAISE(ABORT, 'pagamento inválido: a despesa está anulada')
    WHERE (SELECT cancelled_at FROM expenses WHERE id = NEW.expense_id) IS NOT NULL;
END;

CREATE TABLE IF NOT EXISTS expense_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    expense_id INTEGER NOT NULL REFERENCES expenses (id),
    changed_at TEXT NOT NULL,
    motivo TEXT NOT NULL CHECK (length(trim(motivo)) > 0),
    changed_by TEXT NOT NULL DEFAULT '',
    edit_token TEXT NOT NULL UNIQUE,
    descricao_before TEXT NOT NULL,
    descricao_after TEXT NOT NULL,
    fornecedor_before TEXT NOT NULL DEFAULT '',
    fornecedor_after TEXT NOT NULL DEFAULT '',
    category_before INTEGER NOT NULL REFERENCES expense_categories (id),
    category_after INTEGER NOT NULL REFERENCES expense_categories (id),
    referencia_before TEXT NOT NULL DEFAULT '',
    referencia_after TEXT NOT NULL DEFAULT '',
    data_before TEXT NOT NULL,
    data_after TEXT NOT NULL,
    due_before TEXT,
    due_after TEXT,
    amount_before INTEGER NOT NULL CHECK (amount_before > 0),
    amount_after INTEGER NOT NULL CHECK (amount_after > 0),
    classificacao_before TEXT NOT NULL,
    classificacao_after TEXT NOT NULL,
    notas_before TEXT NOT NULL DEFAULT '',
    notas_after TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_expense_revisions_expense ON expense_revisions (expense_id);

CREATE TABLE IF NOT EXISTS expense_payment_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    payment_id INTEGER NOT NULL REFERENCES expense_payments (id),
    changed_at TEXT NOT NULL,
    motivo TEXT NOT NULL CHECK (length(trim(motivo)) > 0),
    changed_by TEXT NOT NULL DEFAULT '',
    edit_token TEXT NOT NULL UNIQUE,
    amount_before INTEGER NOT NULL CHECK (amount_before > 0),
    amount_after INTEGER NOT NULL CHECK (amount_after > 0),
    paid_on_before TEXT NOT NULL,
    paid_on_after TEXT NOT NULL,
    method_before TEXT NOT NULL,
    method_after TEXT NOT NULL,
    reference_before TEXT NOT NULL DEFAULT '',
    reference_after TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_expense_payment_revisions_payment
    ON expense_payment_revisions (payment_id);

-- Uma despesa só é corrigida pela edição justificada da aplicação: os campos de
-- negócio só mudam se existir a revisão que os autoriza, escrita na mesma
-- transação, com motivo. Anular (cancelled_at) não é correção: é a anulação,
-- com o seu próprio motivo, e não toca nos campos de negócio.
CREATE TRIGGER IF NOT EXISTS despesas_so_se_corrigem_com_motivo
BEFORE UPDATE ON expenses
BEGIN
    SELECT RAISE(ABORT, 'uma despesa só é alterada por uma correção com motivo registado')
    WHERE (OLD.descricao IS NOT NEW.descricao
        OR OLD.fornecedor IS NOT NEW.fornecedor
        OR OLD.category_id IS NOT NEW.category_id
        OR OLD.referencia IS NOT NEW.referencia
        OR OLD.data IS NOT NEW.data
        OR OLD.due_date IS NOT NEW.due_date
        OR OLD.amount_cents IS NOT NEW.amount_cents
        OR OLD.classificacao IS NOT NEW.classificacao
        OR OLD.notas IS NOT NEW.notas)
      AND NOT EXISTS (
        SELECT 1 FROM expense_revisions
        WHERE expense_id = OLD.id AND edit_token = NEW.edit_token
      );
END;

-- Reduzir o valor de uma despesa abaixo do que já foi pago apagaria dinheiro
-- registado. A aplicação recusa antes e explica o que ajustar; este é o travão.
CREATE TRIGGER IF NOT EXISTS despesas_nao_ficam_abaixo_do_pago
BEFORE UPDATE OF amount_cents ON expenses
BEGIN
    SELECT RAISE(ABORT, 'a despesa não pode ficar abaixo do que já foi pago')
    WHERE NEW.amount_cents < (
        SELECT COALESCE(SUM(amount_cents), 0)
        FROM expense_payments
        WHERE expense_id = OLD.id AND cancelled_at IS NULL
    );
END;

-- Uma despesa com pagamentos válidos não é anulada: primeiro anulam-se os
-- pagamentos, com o respetivo motivo, e depois a despesa.
CREATE TRIGGER IF NOT EXISTS despesas_sem_anular_com_pagamentos
BEFORE UPDATE ON expenses
BEGIN
    SELECT RAISE(ABORT, 'uma despesa com pagamentos válidos não é anulada: anula primeiro os pagamentos, com motivo')
    WHERE NEW.cancelled_at IS NOT NULL
      AND OLD.cancelled_at IS NULL
      AND EXISTS (
        SELECT 1 FROM expense_payments
        WHERE expense_id = OLD.id AND cancelled_at IS NULL
      );
END;

-- Uma despesa anulada fica congelada: não é editada nem reativada.
CREATE TRIGGER IF NOT EXISTS despesas_sem_editar_anulada
BEFORE UPDATE ON expenses
BEGIN
    SELECT RAISE(ABORT, 'uma despesa anulada não é editada nem reativada')
    WHERE OLD.cancelled_at IS NOT NULL;
END;

CREATE TRIGGER IF NOT EXISTS despesas_sem_delete
BEFORE DELETE ON expenses
BEGIN
    SELECT RAISE(ABORT, 'uma despesa não é apagada: anula-a e o histórico conserva-se');
END;

CREATE TRIGGER IF NOT EXISTS despesas_sem_alterar_auditoria
BEFORE UPDATE ON expenses
BEGIN
    SELECT RAISE(ABORT, 'identificadores e rasto de auditoria não são campos editáveis')
    WHERE OLD.id IS NOT NEW.id
       OR OLD.created_at IS NOT NEW.created_at
       OR OLD.form_token IS NOT NEW.form_token
       OR (OLD.cancellation_reason IS NOT NULL
           AND OLD.cancellation_reason IS NOT NEW.cancellation_reason);
END;

-- Um pagamento de despesa corrige-se pela mesma via: revisão com motivo, na
-- mesma transação, autorizada pela marca do formulário.
CREATE TRIGGER IF NOT EXISTS pagamentos_de_despesa_so_se_corrigem_com_motivo
BEFORE UPDATE ON expense_payments
BEGIN
    SELECT RAISE(ABORT, 'um pagamento de despesa só é alterado por uma correção com motivo registado')
    WHERE (OLD.amount_cents IS NOT NEW.amount_cents
        OR OLD.paid_on IS NOT NEW.paid_on
        OR OLD.method IS NOT NEW.method
        OR OLD.reference IS NOT NEW.reference)
      AND NOT EXISTS (
        SELECT 1 FROM expense_payment_revisions
        WHERE payment_id = OLD.id AND edit_token = NEW.edit_token
      );
END;

CREATE TRIGGER IF NOT EXISTS pagamentos_de_despesa_sem_editar_anulado
BEFORE UPDATE ON expense_payments
BEGIN
    SELECT RAISE(ABORT, 'um pagamento de despesa anulado não é editado nem reativado')
    WHERE OLD.cancelled_at IS NOT NULL;
END;

CREATE TRIGGER IF NOT EXISTS pagamentos_de_despesa_sem_delete
BEFORE DELETE ON expense_payments
BEGIN
    SELECT RAISE(ABORT, 'um pagamento de despesa não é apagado: anula-o e o histórico conserva-se');
END;

CREATE TRIGGER IF NOT EXISTS pagamentos_de_despesa_sem_alterar_auditoria
BEFORE UPDATE ON expense_payments
BEGIN
    SELECT RAISE(ABORT, 'identificadores e rasto de auditoria não são campos editáveis')
    WHERE OLD.id IS NOT NEW.id
       OR OLD.created_at IS NOT NEW.created_at
       OR OLD.form_token IS NOT NEW.form_token
       OR (OLD.cancellation_reason IS NOT NULL
           AND OLD.cancellation_reason IS NOT NEW.cancellation_reason);
END;

CREATE TRIGGER IF NOT EXISTS correcoes_de_despesa_sem_update
BEFORE UPDATE ON expense_revisions
BEGIN
    SELECT RAISE(ABORT, 'o rasto de correções de uma despesa não é alterado');
END;

CREATE TRIGGER IF NOT EXISTS correcoes_de_despesa_sem_delete
BEFORE DELETE ON expense_revisions
BEGIN
    SELECT RAISE(ABORT, 'o rasto de correções de uma despesa não é apagado');
END;

CREATE TRIGGER IF NOT EXISTS correcoes_de_pagamento_de_despesa_sem_update
BEFORE UPDATE ON expense_payment_revisions
BEGIN
    SELECT RAISE(ABORT, 'o rasto de correções de um pagamento de despesa não é alterado');
END;

CREATE TRIGGER IF NOT EXISTS correcoes_de_pagamento_de_despesa_sem_delete
BEFORE DELETE ON expense_payment_revisions
BEGIN
    SELECT RAISE(ABORT, 'o rasto de correções de um pagamento de despesa não é apagado');
END;

-- ---------------------------------------------------------------------------
-- Entrega 4: integrações (Gmail só de leitura e IA opcional) e documentos.
--
-- A tabela `integracoes` guarda configuração que pode conter segredos (chave
-- OAuth, chave de API). Os valores nunca são apresentados no navegador, nunca
-- vão para a consola nem para o repositório; as páginas mostram só o estado
-- ("guardado", "ligado a …") e a data da última atualização.
-- Os ficheiros dos documentos ficam fora da pasta pública (instance/documentos),
-- com nomes internos seguros; a aplicação é o único acesso.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS integracoes (
    chave TEXT PRIMARY KEY CHECK (length(trim(chave)) > 0),
    valor TEXT NOT NULL DEFAULT '',
    atualizado_em TEXT NOT NULL
);

-- Cópia local de um documento (email guardado, anexo, comprovativo do condómino
-- ou upload direto do administrador — v10).
-- `decisao` é só para comprovativos de condómino: nunca alteram saldos sozinhos.
-- Metadados da biblioteca (v10): título, categoria, data do documento, descrição;
-- para atas também número e data da reunião. `arquivado` esconde da lista
-- principal sem apagar associações nem histórico. `partilha_todos` é partilha
-- explícita com todos os condóminos. Versões: `versao_de` aponta para a versão
-- anterior; a anterior conserva-se sempre — nada é substituído silenciosamente.
CREATE TABLE IF NOT EXISTS documentos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    origem TEXT NOT NULL CHECK (origem IN ('email', 'anexo', 'condomino', 'upload')),
    nome_interno TEXT NOT NULL UNIQUE CHECK (length(trim(nome_interno)) > 0),
    nome_original TEXT NOT NULL DEFAULT '',
    mime TEXT NOT NULL DEFAULT '',
    tamanho_bytes INTEGER NOT NULL CHECK (tamanho_bytes >= 0),
    sha256 TEXT NOT NULL DEFAULT '',
    -- Referência à origem Gmail (quando veio do email): livre e opcional.
    gmail_mensagem_id TEXT NOT NULL DEFAULT '',
    gmail_anexo_id TEXT NOT NULL DEFAULT '',
    resumo TEXT NOT NULL DEFAULT '',
    titulo TEXT NOT NULL DEFAULT '',
    categoria TEXT NOT NULL DEFAULT 'Outro'
        CHECK (categoria IN ('Ata', 'Fatura', 'Extrato bancário', 'Contrato',
                             'Seguro', 'Orçamento', 'Comprovativo', 'Outro')),
    data_documento TEXT NOT NULL DEFAULT '',
    descricao TEXT NOT NULL DEFAULT '',
    ata_numero TEXT NOT NULL DEFAULT '',
    ata_data TEXT NOT NULL DEFAULT '',
    arquivado INTEGER NOT NULL DEFAULT 0 CHECK (arquivado IN (0, 1)),
    partilha_todos INTEGER NOT NULL DEFAULT 0 CHECK (partilha_todos IN (0, 1)),
    versao INTEGER NOT NULL DEFAULT 1 CHECK (versao >= 1),
    versao_de INTEGER REFERENCES documentos (id),
    tratado_em TEXT,
    decisao TEXT NOT NULL DEFAULT 'guardado'
        CHECK (decisao IN ('guardado', 'pendente', 'confirmado', 'rejeitado')),
    decisao_motivo TEXT NOT NULL DEFAULT '',
    decidido_em TEXT,
    decidido_por TEXT NOT NULL DEFAULT '',
    criado_em TEXT NOT NULL,
    criado_por TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_documentos_origem ON documentos (origem);
CREATE INDEX IF NOT EXISTS idx_documentos_decisao ON documentos (decisao);
CREATE INDEX IF NOT EXISTS idx_documentos_categoria ON documentos (categoria);
CREATE INDEX IF NOT EXISTS idx_documentos_sha256 ON documentos (sha256);
CREATE INDEX IF NOT EXISTS idx_documentos_versao_de ON documentos (versao_de);

-- Ligação de um documento a um registo (financeiro ou fração). A ligação é um
-- registo próprio: associar não cria nem altera movimentos.
CREATE TABLE IF NOT EXISTS documento_ligacoes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    documento_id INTEGER NOT NULL REFERENCES documentos (id),
    alvo TEXT NOT NULL CHECK (alvo IN ('pagamento', 'pagamento_despesa', 'despesa', 'fracao')),
    alvo_id INTEGER NOT NULL,
    criado_em TEXT NOT NULL,
    criado_por TEXT NOT NULL DEFAULT '',
    notas TEXT NOT NULL DEFAULT '',
    UNIQUE (documento_id, alvo, alvo_id)
);

CREATE INDEX IF NOT EXISTS idx_documento_ligacoes_alvo ON documento_ligacoes (alvo, alvo_id);

-- Partilha explícita de um documento com frações. Nada é partilhado automaticamente.
CREATE TABLE IF NOT EXISTS documento_fracoes (
    documento_id INTEGER NOT NULL REFERENCES documentos (id),
    fraction_id INTEGER NOT NULL REFERENCES fractions (id),
    criado_em TEXT NOT NULL,
    PRIMARY KEY (documento_id, fraction_id)
);

-- Um documento ligado a um registo não é apagado: o rasto conserva-se.
CREATE TRIGGER IF NOT EXISTS documentos_sem_apagar_os_ligados
BEFORE DELETE ON documentos
WHEN EXISTS (SELECT 1 FROM documento_ligacoes WHERE documento_id = OLD.id)
BEGIN
    SELECT RAISE(ABORT, 'documento ligado a um registo não é apagado: o rasto conserva-se');
END;

CREATE TRIGGER IF NOT EXISTS documento_ligacoes_sem_apagar
BEFORE DELETE ON documento_ligacoes
BEGIN
    SELECT RAISE(ABORT, 'a ligação de um documento a um registo não é apagada');
END;

-- ---------------------------------------------------------------------------
-- Automatização bancária (v11): consultas, extrações, movimentos e regras.
-- Nada aqui escreve dinheiro: os registos passam pelas mesmas funções,
-- transações e travões usados pelos formulários manuais.
-- ---------------------------------------------------------------------------

-- Um ciclo de cada vez. Uma execução interrompida fica marcada e o retoma
-- é seguro: as extrações e os movimentos têm marcas únicas.
CREATE TABLE IF NOT EXISTS processamentos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    iniciado_em TEXT NOT NULL,
    terminado_em TEXT,
    estado TEXT NOT NULL DEFAULT 'a_decorrer'
        CHECK (estado IN ('a_decorrer', 'concluido', 'interrompido', 'erro')),
    gatilho TEXT NOT NULL DEFAULT 'manual' CHECK (gatilho IN ('manual', 'agendado')),
    resumo_json TEXT NOT NULL DEFAULT '{}',
    erro TEXT NOT NULL DEFAULT ''
);

-- Emails já vistos pela automatização (não repetir consultas nem registos).
CREATE TABLE IF NOT EXISTS auto_mensagens (
    mensagem_id TEXT PRIMARY KEY,
    processado_em TEXT NOT NULL,
    estado TEXT NOT NULL DEFAULT 'processada'
        CHECK (estado IN ('processada', 'sem_anexos', 'autenticacao_recusada', 'erro')),
    nota TEXT NOT NULL DEFAULT ''
);

-- O que foi extraído de cada documento, com a versão do extrator para
-- diagnóstico e reprocessamento. Uma extração por linha de documento
-- (nova versão = nova linha = nova extração).
CREATE TABLE IF NOT EXISTS extracoes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    documento_id INTEGER NOT NULL REFERENCES documentos (id),
    processamento_id INTEGER REFERENCES processamentos (id),
    extrator TEXT NOT NULL DEFAULT '',
    versao_extrator INTEGER NOT NULL DEFAULT 0,
    campos_json TEXT NOT NULL DEFAULT '[]',
    origem_json TEXT NOT NULL DEFAULT '[]',
    estado TEXT NOT NULL DEFAULT 'revisao'
        CHECK (estado IN ('reconhecido', 'revisao', 'sem_movimentos', 'falha')),
    nota TEXT NOT NULL DEFAULT '',
    criado_em TEXT NOT NULL,
    UNIQUE (documento_id)
);

-- Identidade do movimento: banco + data + valor + sentido + contraparte +
-- referência + ocorrência no documento. O mesmo movimento num comprovativo e
-- num extrato dá o mesmo hash e conta uma única vez; dois movimentos legítimos
-- exatamente iguais têm ocorrências diferentes.
CREATE TABLE IF NOT EXISTS auto_movimentos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    extracao_id INTEGER NOT NULL REFERENCES extracoes (id),
    hash TEXT NOT NULL UNIQUE,
    sentido TEXT NOT NULL CHECK (sentido IN ('recebido', 'enviado', 'comissao')),
    estado TEXT NOT NULL DEFAULT 'proposta'
        CHECK (estado IN ('proposta', 'registado', 'revisao', 'ignorado')),
    payment_id INTEGER REFERENCES payments (id),
    expense_payment_id INTEGER REFERENCES expense_payments (id),
    regra TEXT NOT NULL DEFAULT '',
    nota TEXT NOT NULL DEFAULT '',
    criado_em TEXT NOT NULL
);

-- Correspondências definidas pelo administrador: IBAN/ordenante/referência
-- → fração ou fornecedor. 「comissao」 a 1 é a regra explícita que autoriza
-- criar despesa por comissões bancárias.
CREATE TABLE IF NOT EXISTS auto_correspondencias (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tipo TEXT NOT NULL CHECK (tipo IN ('iban', 'ordenante', 'referencia')),
    padrao TEXT NOT NULL CHECK (trim(padrao) <> ''),
    alvo_tipo TEXT NOT NULL CHECK (alvo_tipo IN ('fracao', 'fornecedor')),
    alvo_id INTEGER REFERENCES fractions (id),
    alvo_texto TEXT NOT NULL DEFAULT '',
    comissao INTEGER NOT NULL DEFAULT 0,
    ativo INTEGER NOT NULL DEFAULT 1,
    criado_em TEXT NOT NULL,
    UNIQUE (tipo, padrao),
    CHECK (
        (alvo_tipo = 'fracao' AND alvo_id IS NOT NULL)
        OR (alvo_tipo = 'fornecedor' AND trim(alvo_texto) <> '')
    )
);
