"""Entrega 4: Email via Gmail — fluxo OAuth, lista, corpo sanitizado e anexos.

Nenhum teste toca em serviços reais: `app.gmail.pedido_json` é substituído por
um Gmail falso que devolve respostas simuladas e regista os pedidos feitos.
"""

from __future__ import annotations

import base64

import pytest

from conftest import protecao_de


@pytest.fixture(autouse=True)
def _cache_email_limpo():
    """A cache de Email é por processo: nenhum teste herda resultados do anterior."""
    from app.gmail import _limpar_caches

    _limpar_caches()


def _urlsafe(dados: bytes) -> str:
    return base64.urlsafe_b64encode(dados).decode("ascii").rstrip("=")


class GmailFalso:
    """Substitui app.gmail.pedido_json: devolve respostas fixas e regista pedidos."""

    def __init__(self, rotas: dict[str, dict]):
        self.rotas = rotas
        self.pedidos: list[tuple[str, str]] = []

    def __call__(self, url, *, metodo="GET", headers=None, payload=None, timeout=20):
        self.pedidos.append((metodo, url))
        for chave, resposta in self.rotas.items():
            if chave in url:
                return resposta
        return 404, {}


@pytest.fixture
def gmail_ligado(app, client, monkeypatch):
    """Credenciais e tokens guardados + transporte falso. Devolve o Gmail falso."""
    from app.integracoes import guardar_integracao

    rotas = {
        "/profile": (200, {"emailAddress": "condominio@example.com"}),
        "/messages?maxResults=20": (
            200,
            {
                # Contrato real da listagem: só id + threadId por mensagem.
                "messages": [{"id": "m1", "threadId": "t1"}, {"id": "m2", "threadId": "t2"}],
                "nextPageToken": "pagina-2",
                "resultSizeEstimate": 26,
            },
        ),
        "/messages/m1?format=metadata": (
            200,
            {
                # Contrato real do messages.get com format=metadata: só cabeçalhos.
                "id": "m1",
                "payload": {"headers": [
                    {"name": "From", "value": "empresa-x@example.com"},
                    {"name": "Subject", "value": "Fatura 42 — Limpeza"},
                    {"name": "Date", "value": "10/01/2027"},
                ]},
            },
        ),
        "/messages/m2?format=metadata": (
            200,
            {
                "id": "m2",
                "payload": {"headers": [
                    {"name": "From", "value": "banco@example.com"},
                    {"name": "Subject", "value": "Extrato janeiro"},
                    {"name": "Date", "value": "12/01/2027"},
                ]},
            },
        ),
    }
    falso = GmailFalso(rotas)
    monkeypatch.setattr("app.gmail.pedido_json", falso)

    with app.app_context():
        guardar_integracao("gmail_client_id", "cliente-teste")
        guardar_integracao("gmail_client_secret", "segredo-teste")
        guardar_integracao("gmail_refresh_token", "refresh-teste")
        guardar_integracao("gmail_access_token", "access-teste")
        guardar_integracao("gmail_access_token_expira", "9999999999")
    return falso


# ------------------------------------------------------------------ ligação ---


def test_sem_ligacao_o_email_aponta_para_as_integracoes(app, client, db):
    resposta = client.get("/email", follow_redirects=True)
    html = resposta.get_data(as_text=True)
    assert "ainda não está ligado" in html
    assert "Definições → Integrações" in html


def test_ligar_redireciona_para_o_google_so_com_leitura_e_state(app, client, db):
    from app.integracoes import guardar_integracao

    with app.app_context():
        guardar_integracao("gmail_client_id", "cliente-teste")
        guardar_integracao("gmail_client_secret", "segredo-teste")

    resposta = client.post(
        "/email/oauth/ligar",
        data={"csrf_token": _csrf(client, "/definicoes/integracoes")},
    )
    assert resposta.status_code == 302
    destino = resposta.headers["Location"]
    assert destino.startswith("https://accounts.google.com/")
    assert "gmail.readonly" in destino          # só de leitura
    assert "access_type=offline" in destino     # permite reconectar sem nova visita
    assert "redirect_uri=http%3A%2F%2Flocalhost%2Femail%2Foauth%2Fcallback" in destino
    assert "state=" in destino


def _csrf(client, caminho):
    import re

    html = client.get(caminho).get_data(as_text=True)
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def test_callback_troca_codigo_por_tokens_e_state_falso_e_recusado(app, client, db, monkeypatch):
    from app.integracoes import guardar_integracao, obter_integracao

    with app.app_context():
        guardar_integracao("gmail_client_id", "cliente-teste")
        guardar_integracao("gmail_client_secret", "segredo-teste")

    falso = GmailFalso({"/token": (200, {
        "access_token": "access-novo",
        "refresh_token": "refresh-novo",
        "expires_in": 3600,
    })})
    monkeypatch.setattr("app.gmail.pedido_json", falso)

    # Iniciar a ligação para obter o state, como o navegador faria.
    resposta = client.post("/email/oauth/ligar", data={"csrf_token": _csrf(client, "/definicoes/integracoes")})
    import re

    state = re.search(r"state=([^&]+)", resposta.headers["Location"]).group(1)

    resposta = client.get(f"/email/oauth/callback?code=codigo-123&state={state}")
    assert resposta.status_code == 302
    with app.app_context():
        assert obter_integracao("gmail_access_token") == "access-novo"
        assert obter_integracao("gmail_refresh_token") == "refresh-novo"

    # Um state que não foi iniciado por esta sessão não vale.
    resposta = client.get("/email/oauth/callback?code=codigo-999&state=state-inventado")
    with app.app_context():
        assert obter_integracao("gmail_access_token") == "access-novo"  # nada mudou
    html = client.get("/definicoes/integracoes").get_data(as_text=True)
    assert "não corresponde a uma ligação iniciada" in html


def test_desligar_remove_tokens_mas_conserva_credenciais(app, client, db, monkeypatch):
    from app.integracoes import guardar_integracao, obter_integracao

    with app.app_context():
        for chave, valor in (
            ("gmail_client_id", "cliente-teste"),
            ("gmail_client_secret", "segredo-teste"),
            ("gmail_refresh_token", "refresh-teste"),
            ("gmail_access_token", "access-teste"),
        ):
            guardar_integracao(chave, valor)

    falso = GmailFalso({"/revoke": (200, {})})
    monkeypatch.setattr("app.gmail.pedido_json", falso)

    client.post("/email/desligar", data={"csrf_token": _csrf(client, "/definicoes/integracoes")},
                follow_redirects=True)
    with app.app_context():
        assert obter_integracao("gmail_refresh_token") == ""
        assert obter_integracao("gmail_access_token") == ""
        assert obter_integracao("gmail_client_id") == "cliente-teste"


def test_testar_ligacao_confirma_a_conta(app, client, db, gmail_ligado):
    client.post("/email/testar", data={"csrf_token": _csrf(client, "/definicoes/integracoes")},
                follow_redirects=True)
    from app.integracoes import obter_integracao

    with app.app_context():
        assert obter_integracao("gmail_conta") == "condominio@example.com"
    html = client.get("/definicoes/integracoes").get_data(as_text=True)
    assert "condominio@example.com" in html


# -------------------------------------------------------------------- lista ---


def test_lista_mostra_assuntos_e_pesquisa_filtra_por_datas(app, client, db, gmail_ligado):
    html = client.get("/email").get_data(as_text=True)
    assert "Fatura 42 — Limpeza" in html
    assert "Extrato janeiro" in html

    client.get("/email?q=limpeza&de=01/01/2027&ate=31/01/2027").get_data(as_text=True)
    pedidos_pesquisa = [url for _, url in gmail_ligado.pedidos if "maxResults" in url]
    assert any("q=limpeza" in url for url in pedidos_pesquisa)
    assert any("after%3A2027%2F01%2F01" in url for url in pedidos_pesquisa)
    assert any("before%3A2027%2F01%2F31" in url for url in pedidos_pesquisa)


def test_paginacao_usa_o_token_do_gmail(app, client, db, gmail_ligado):
    html = client.get("/email").get_data(as_text=True)
    assert "pagina-2" in html  # ligação para a página seguinte
    client.get("/email?pagina=pagina-2").get_data(as_text=True)
    assert any("pageToken=pagina-2" in url for _, url in gmail_ligado.pedidos)


def test_a_aplicacao_nunca_escreve_no_gmail(app, client, db, gmail_ligado):
    """Só de leitura: nenhum POST à API do Gmail (OAuth à parte é ligar/desligar)."""
    client.get("/email").get_data(as_text=True)
    client.get("/email/m1").get_data(as_text=True)
    escritas = [
        (metodo, url)
        for metodo, url in gmail_ligado.pedidos
        if metodo != "GET" and "gmail.googleapis.com" in url
    ]
    assert escritas == []


# ------------------------------------------------- desempenho da lista --------


def test_lista_pedira_20_mensagens_e_so_o_necessario(app, client, db, gmail_ligado):
    client.get("/email").get_data(as_text=True)
    listas = [url for _, url in gmail_ligado.pedidos if "/messages?" in url]
    assert listas, "a lista não pediu mensagens ao Gmail"
    assert "maxResults=20" in listas[0]
    assert "fields=" in listas[0]  # só o que a listagem realmente devolve
    # A listagem documentada só devolve id + threadId: pedir snippet ou
    # prevPageToken nos fields é o que leva o Gmail a recusar (400).
    assert "snippet" not in listas[0]
    assert "prevPageToken" not in listas[0]
    detalhes = [url for _, url in gmail_ligado.pedidos if "format=metadata" in url]
    assert detalhes and all("metadataHeaders" in url for url in detalhes)
    assert all("format=full" not in url for url in detalhes)  # sem corpos na lista


def test_lista_repetida_aproveita_a_cache_curta(app, client, db, gmail_ligado):
    html_1 = client.get("/email").get_data(as_text=True)
    pedidos_1 = len(gmail_ligado.pedidos)
    html_2 = client.get("/email").get_data(as_text=True)
    assert "Fatura 42 — Limpeza" in html_2
    assert len(gmail_ligado.pedidos) == pedidos_1, "a repetição voltou ao Gmail"

    # 「Atualizar」 força um pedido novo.
    client.get("/email?atualizar=1").get_data(as_text=True)
    assert len(gmail_ligado.pedidos) > pedidos_1


def test_desligar_limpa_a_cache(app, client, db, gmail_ligado, monkeypatch):
    client.get("/email").get_data(as_text=True)
    assert gmail_ligado.pedidos
    pedidos_1 = len(gmail_ligado.pedidos)
    monkeypatch.setattr("app.gmail.pedido_json", GmailFalso({"/revoke": (200, {})}))
    client.post("/email/desligar", data={"csrf_token": _csrf(client, "/definicoes/integracoes")},
                follow_redirects=True)
    from app.gmail import _cache_lista

    assert _cache_lista == {}


def test_aviso_de_carregamento_chega_antes_do_pedido_ao_gmail(app, client, db, gmail_ligado):
    """A estrutura da página é enviada antes dos resultados: o primeiro bloco
    do fluxo não depende de o Gmail ter respondido."""
    from app import gmail

    with app.test_request_context("/email"):
        fluxo = gmail._resultados_email(
            "cabecalho", "", {"maxResults": 20}, ("conta-teste", ()), True
        )
        primeiro = next(fluxo)
        assert "A consultar o Gmail" in primeiro
        assert not any("/messages?" in url for _, url in gmail_ligado.pedidos)
        segundo = next(fluxo)
        assert "Fatura 42 — Limpeza" in segundo
        assert any("/messages?" in url for _, url in gmail_ligado.pedidos)


# ------------------------------------------------- erro e fluxo ---------------


def _ligacao_manual(app, monkeypatch, rotas):
    """Ligação guardada + transporte falso com rotas à escolha do teste."""
    from app.integracoes import guardar_integracao

    with app.app_context():
        for chave, valor in (
            ("gmail_client_id", "c"), ("gmail_client_secret", "s"),
            ("gmail_refresh_token", "r"), ("gmail_access_token", "a"),
            ("gmail_access_token_expira", "9999999999"),
        ):
            guardar_integracao(chave, valor)
    falso = GmailFalso(rotas)
    monkeypatch.setattr("app.gmail.pedido_json", falso)
    return falso


def test_400_do_gmail_mostra_erro_controlado_e_termina_o_carregamento(app, client, db, monkeypatch):
    """Recusa do Gmail: aviso renderizado pela aplicação (motivo estruturado
    da resposta, sem expor tokens/conteúdo), nunca HTML literal, e o estado de
    carregamento termina também no erro."""
    _ligacao_manual(app, monkeypatch, {
        "/messages?maxResults=": (400, {
            "error": {
                "code": 400,
                "status": "INVALID_ARGUMENT",
                "message": "Invalid field selection for users.messages.list.",
            }
        }),
    })
    html = client.get("/email").get_data(as_text=True)
    assert "O Gmail recusou o pedido" in html
    assert "INVALID_ARGUMENT" in html          # motivo estruturado, da resposta
    assert 'class="aviso-config"' in html      # apresentado como elemento, não texto
    assert "&lt;p" not in html                 # sem HTML impresso literalmente
    assert "fim-carregamento" in html          # carregamento terminou no erro
    assert "Sem emails com estes filtros." not in html  # recusa não é lista vazia


def test_dados_externos_continuam_escapados_no_fluxo(app, client, db, gmail_ligado):
    """Assunto/remetente hostis vindos do Gmail nunca viramo HTML na página."""
    gmail_ligado.rotas["/messages/m1?format=metadata"] = (200, {
        "id": "m1",
        "payload": {"headers": [
            {"name": "From", "value": "<script>alert(1)</script>@x.com"},
            {"name": "Subject", "value": "Assunto <b>perigoso</b>"},
            {"name": "Date", "value": "10/01/2027"},
        ]},
    })
    html = client.get("/email").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in html
    assert "Assunto &lt;b&gt;perigoso&lt;/b&gt;" in html


def test_lista_vazia_e_distinta_de_pedido_recusado(app, client, db, monkeypatch):
    _ligacao_manual(app, monkeypatch, {
        "/messages?maxResults=": (200, {"messages": [], "resultSizeEstimate": 0}),
    })
    html = client.get("/email").get_data(as_text=True)
    assert "Sem emails com estes filtros." in html
    assert "aviso-config" not in html
    assert "fim-carregamento" in html


def test_falha_inesperada_durante_o_fluxo_termina_com_erro(app, client, db, monkeypatch):
    """Se o transporte rebentar a meio do fluxo, a página acaba com erro
    controlado em vez de resposta partida e carregamento eterno."""
    class GmailQueExplode:
        def __call__(self, url, **kwargs):
            if "/messages?" in url:
                raise ConnectionError("rede cortada")
            return 404, {}

    _ligacao_manual(app, monkeypatch, {})
    monkeypatch.setattr("app.gmail.pedido_json", GmailQueExplode())
    resposta = client.get("/email")
    assert resposta.status_code == 200
    html = resposta.get_data(as_text=True)
    assert "aviso-config" in html
    assert "fim-carregamento" in html


def test_abrir_mensagem_evita_repedir_a_mensagem_para_o_anexo(app, client, db, monkeypatch):
    from app.integracoes import guardar_integracao

    with app.app_context():
        for chave, valor in (
            ("gmail_client_id", "c"), ("gmail_client_secret", "s"),
            ("gmail_refresh_token", "r"), ("gmail_access_token", "a"),
            ("gmail_access_token_expira", "9999999999"),
        ):
            guardar_integracao(chave, valor)

    pdf = b"%PDF-1.4 conteudo falso de teste"
    falso = GmailFalso({
        "/messages/m1/attachments/anexo1": (200, {"data": _urlsafe(pdf)}),
        "/messages/m1?format=full": (
            200,
            {
                "id": "m1",
                "threadId": "m1",
                "payload": {
                    "headers": [{"name": "Subject", "value": "Fatura"}],
                    "parts": [{
                        # Estrutura real: filename na parte; attachmentId no body.
                        "mimeType": "application/pdf",
                        "filename": "fatura-42.pdf",
                        "body": {"attachmentId": "anexo1", "size": len(pdf)},
                    }],
                },
            },
        ),
    })
    monkeypatch.setattr("app.gmail.pedido_json", falso)

    client.get("/email/m1")  # abre a mensagem: os metadados dos anexos ficam em cache
    completos_1 = sum(1 for _, url in falso.pedidos if "format=full" in url)
    resposta = client.get("/email/m1/anexo/anexo1")
    assert resposta.status_code == 200
    completos_2 = sum(1 for _, url in falso.pedidos if "format=full" in url)
    assert completos_2 == completos_1, "o anexo voltou a descarregar a mensagem inteira"


# ------------------------------------------------------------------- corpo ----


def _mensagem_com_html(html_corpo: str) -> GmailFalso:
    return GmailFalso({
        "/messages/m1?format=full": (
            200,
            {
                "id": "m1",
                "threadId": "t1",
                "payload": {
                    "headers": [
                        {"name": "From", "value": "empresa-x@example.com"},
                        {"name": "Subject", "value": "Fatura 42"},
                        {"name": "To", "value": "condominio@example.com"},
                        {"name": "Date", "value": "10/01/2027"},
                    ],
                    "parts": [
                        {"mimeType": "text/html", "body": {"data": _urlsafe(html_corpo.encode("utf-8"))}},
                    ],
                },
            },
        )
    })


def test_corpo_html_e_apresentado_sanitizado_sem_scripts_nem_conteudo_remoto(app, client, db, monkeypatch):
    from app.integracoes import guardar_integracao

    with app.app_context():
        for chave, valor in (
            ("gmail_client_id", "c"), ("gmail_client_secret", "s"),
            ("gmail_refresh_token", "r"), ("gmail_access_token", "a"),
            ("gmail_access_token_expira", "9999999999"),
        ):
            guardar_integracao(chave, valor)
    monkeypatch.setattr("app.gmail.pedido_json", _mensagem_com_html(
        '<p>Pagamento <script>alert(1)</script> recebido '
        '<img src="https://remoto.example.com/pixel.png" onerror="alert(2)">'
        '<a href="https://remoto.example.com">link</a></p>'
    ))

    html = client.get("/email/m1").get_data(as_text=True)
    assert "alert(1)" not in html
    assert "remoto.example.com" not in html  # sem imagens nem links remotos
    assert "Pagamento" in html and "recebido" in html

    # O corpo apresentado (o bloco isolado) não tem tags de script nem atributos.
    import re as _re

    corpo_apresentado = _re.search(
        r'<div class="corpo-email">(.*)</div>', html, _re.DOTALL
    ).group(1)
    assert "<script" not in corpo_apresentado.lower()
    assert "<img" not in corpo_apresentado.lower()
    assert "onerror" not in corpo_apresentado.lower()


# ------------------------------------------------------------------ anexos ----


def test_anexo_descarregado_e_pdf_abre_no_navegador(app, client, db, monkeypatch):
    from app.integracoes import guardar_integracao

    with app.app_context():
        for chave, valor in (
            ("gmail_client_id", "c"), ("gmail_client_secret", "s"),
            ("gmail_refresh_token", "r"), ("gmail_access_token", "a"),
            ("gmail_access_token_expira", "9999999999"),
        ):
            guardar_integracao(chave, valor)

    pdf = b"%PDF-1.4 conteudo falso de teste"
    falso = GmailFalso({
        "/messages/m1/attachments/anexo1": (200, {"data": _urlsafe(pdf)}),
        "/messages/m1?format=full": (
            200,
            {
                "id": "m1",
                "threadId": "m1",
                "payload": {
                    "headers": [{"name": "Subject", "value": "Fatura"}],
                    "parts": [{
                        # Estrutura real: filename na parte; attachmentId no body.
                        "mimeType": "application/pdf",
                        "filename": "fatura-42.pdf",
                        "body": {"attachmentId": "anexo1", "size": len(pdf)},
                    }],
                },
            },
        ),
    })
    monkeypatch.setattr("app.gmail.pedido_json", falso)

    resposta = client.get("/email/m1/anexo/anexo1")
    assert resposta.status_code == 200
    assert resposta.data == pdf
    assert resposta.headers["Content-Type"].startswith("application/pdf")
    assert "inline" in resposta.headers["Content-Disposition"]  # abre no navegador


def test_anexo_demasiado_grande_e_recusado(app, client, db, monkeypatch):
    from app.integracoes import guardar_integracao
    from app.gmail import MAX_ANEXO_BYTES

    with app.app_context():
        for chave, valor in (
            ("gmail_client_id", "c"), ("gmail_client_secret", "s"),
            ("gmail_refresh_token", "r"), ("gmail_access_token", "a"),
            ("gmail_access_token_expira", "9999999999"),
        ):
            guardar_integracao(chave, valor)

    grande = b"x" * (MAX_ANEXO_BYTES + 1)
    falso = GmailFalso({
        "/messages/m1/attachments/anexo1": (200, {"data": _urlsafe(grande)}),
    })
    monkeypatch.setattr("app.gmail.pedido_json", falso)

    assert client.get("/email/m1/anexo/anexo1").status_code == 413


# ---------------------------------- estrutura MIME real (diagnosticada) -------


def _mensagem_real(*, pdf: bytes, thread: str = "m1", anexos_payload: dict | None = None,
                   inline: bool = False) -> dict:
    """Mensagem com a estrutura real de `format=full`: filename na parte e
    attachmentId/size no body (como o Gmail devolve, confirmado por diagnóstico)."""
    if inline:
        parte = {
            "mimeType": "application/pdf",
            "filename": "fatura-inline.pdf",
            "body": {"data": _urlsafe(pdf), "size": len(pdf)},
        }
    else:
        parte = {
            "mimeType": "application/pdf",
            "filename": "fatura-42.pdf",
            "body": {"attachmentId": "anexo1", "size": len(pdf)},
        }
    msg = {
        "id": "m1",
        "threadId": thread,
        "payload": {
            "headers": [
                {"name": "From", "value": "empresa-x@example.com"},
                {"name": "Subject", "value": "Fatura 42 — Limpeza"},
                {"name": "Date", "value": "10/01/2027"},
            ],
            "parts": [
                {"mimeType": "text/html", "body": {"data": _urlsafe(b"<p>fatura</p>")}},
                parte,
            ],
        },
    }
    if anexos_payload is not None:
        msg["payload"] = anexos_payload
    return msg


def test_anexo_com_body_attachmentid_e_listado_ao_abrir_a_mensagem(app, client, db, monkeypatch):
    """O caso real: attachmentId no body (não existe `body.attachment`)."""
    from app.integracoes import guardar_integracao

    with app.app_context():
        for chave, valor in (
            ("gmail_client_id", "c"), ("gmail_client_secret", "s"),
            ("gmail_refresh_token", "r"), ("gmail_access_token", "a"),
            ("gmail_access_token_expira", "9999999999"),
        ):
            guardar_integracao(chave, valor)
    pdf = b"%PDF-1.4 conteudo falso de teste"
    monkeypatch.setattr("app.gmail.pedido_json", GmailFalso({
        "/messages/m1?format=full": (200, _mensagem_real(pdf=pdf)),
    }))

    html = client.get("/email/m1").get_data(as_text=True)
    assert "fatura-42.pdf" in html
    assert "application/pdf" in html
    assert "Sem anexos nesta mensagem" not in html


def test_anexo_aninhado_e_anexo_na_raiz_do_payload_sao_identificados():
    from app.gmail import _anexos_da_mensagem

    msg = {
        "payload": {
            "mimeType": "multipart/mixed",
            "parts": [
                {"mimeType": "multipart/related", "parts": [
                    {"mimeType": "text/html", "body": {"data": "x"}},
                    {"mimeType": "application/pdf", "filename": "nested.pdf",
                     "body": {"attachmentId": "an-nested", "size": 10}},
                ]},
            ],
        }
    }
    anexos = _anexos_da_mensagem(msg)
    assert [a["attachment_id"] for a in anexos] == ["an-nested"]

    # Mensagem de uma só parte: o anexo está na raiz do payload.
    raiz = {"payload": {"mimeType": "application/pdf", "filename": "raiz.pdf",
                        "body": {"attachmentId": "an-raiz", "size": 7}}}
    anexos = _anexos_da_mensagem(raiz)
    assert [a["attachment_id"] for a in anexos] == ["an-raiz"]
    assert anexos[0]["nome"] == "raiz.pdf"


def test_pdf_inline_com_body_data_e_listado_servido_e_guardado(app, client, db, monkeypatch):
    """PDF inline (bytes diretamente em body.data, sem attachmentId)."""
    from app.integracoes import guardar_integracao

    with app.app_context():
        for chave, valor in (
            ("gmail_client_id", "c"), ("gmail_client_secret", "s"),
            ("gmail_refresh_token", "r"), ("gmail_access_token", "a"),
            ("gmail_access_token_expira", "9999999999"),
        ):
            guardar_integracao(chave, valor)
    pdf = b"%PDF-1.4 inline falso"
    monkeypatch.setattr("app.gmail.pedido_json", GmailFalso({
        "/messages/m1?format=full": (200, _mensagem_real(pdf=pdf, inline=True)),
    }))

    html = client.get("/email/m1").get_data(as_text=True)
    assert "fatura-inline.pdf" in html

    resposta = client.get("/email/m1/anexo/parte:1")
    assert resposta.status_code == 200
    assert resposta.data == pdf
    assert resposta.headers["Content-Type"].startswith("application/pdf")

    resposta = client.post(
        "/email/m1/anexo/parte:1/guardar",
        data={"csrf_token": _csrf(client, "/email/m1")},
        follow_redirects=True,
    )
    assert "documento local" in resposta.get_data(as_text=True)
    linhas = db.rows("SELECT * FROM documentos")
    assert len(linhas) == 1
    assert linhas[0]["origem"] == "anexo"


def test_conversa_indica_onde_estao_os_anexos_sem_os_atribuir_à_mensagem_errada(
    app, client, db, monkeypatch
):
    """O Gmail agrupa mensagens: o PDF pode estar noutra mensagem da conversa."""
    from app.integracoes import guardar_integracao

    with app.app_context():
        for chave, valor in (
            ("gmail_client_id", "c"), ("gmail_client_secret", "s"),
            ("gmail_refresh_token", "r"), ("gmail_access_token", "a"),
            ("gmail_access_token_expira", "9999999999"),
        ):
            guardar_integracao(chave, valor)
    pdf = b"%PDF-1.4 conteudo falso de teste"
    sem_anexo = {
        "id": "m1", "threadId": "t1",
        "payload": {
            "headers": [{"name": "Subject", "value": "Fatura"}],
            "parts": [{"mimeType": "text/html", "body": {"data": _urlsafe(b"<p>corpo</p>")}}],
        },
    }
    com_anexo = {
        "id": "m2",
        "payload": {
            "headers": [{"name": "Subject", "value": "Fatura"}],
            "parts": [{
                "mimeType": "application/pdf", "filename": "fatura-42.pdf",
                "body": {"attachmentId": "anexo1", "size": len(pdf)},
            }],
        },
    }
    monkeypatch.setattr("app.gmail.pedido_json", GmailFalso({
        "/messages/m1?format=full": (200, sem_anexo),
        "/messages/m2?format=full": (200, com_anexo),
        "/threads/t1": (200, {"id": "t1", "messages": [sem_anexo, com_anexo]}),
    }))

    html = client.get("/email/m1").get_data(as_text=True)
    assert "Sem anexos nesta mensagem" in html
    assert "1 anexo(s)" in html  # a conversa indica onde está o anexo

    html2 = client.get("/email/m2").get_data(as_text=True)
    assert "fatura-42.pdf" in html2  # a outra mensagem mostra o seu anexo


def test_estrutura_inesperada_nao_aparece_como_sem_anexos(app, client, db, monkeypatch):
    """Falha de leitura não é 'sem anexos': o erro real é apresentado."""
    from app.integracoes import guardar_integracao

    with app.app_context():
        for chave, valor in (
            ("gmail_client_id", "c"), ("gmail_client_secret", "s"),
            ("gmail_refresh_token", "r"), ("gmail_access_token", "a"),
            ("gmail_access_token_expira", "9999999999"),
        ):
            guardar_integracao(chave, valor)
    monkeypatch.setattr("app.gmail.pedido_json", GmailFalso({
        "/messages/m1?format=full": (200, {"id": "m1", "threadId": "m1"}),  # sem payload
    }))

    html = client.get("/email/m1").get_data(as_text=True)
    assert "não puderam ser verificados" in html
    assert "Sem anexos nesta mensagem" not in html


def test_pdf_identificado_descarregado_e_guardado_sem_movimentos_financeiros(
    app, client, db, monkeypatch
):
    """Percurso completo com a estrutura real: abrir → descarregar → guardar."""
    from app.integracoes import guardar_integracao

    with app.app_context():
        for chave, valor in (
            ("gmail_client_id", "c"), ("gmail_client_secret", "s"),
            ("gmail_refresh_token", "r"), ("gmail_access_token", "a"),
            ("gmail_access_token_expira", "9999999999"),
        ):
            guardar_integracao(chave, valor)
    pdf = b"%PDF-1.4 fatura falsa de teste"
    monkeypatch.setattr("app.gmail.pedido_json", GmailFalso({
        "/messages/m1?format=full": (200, _mensagem_real(pdf=pdf)),
        "/messages/m1/attachments/anexo1": (200, {"data": _urlsafe(pdf)}),
    }))

    # 1) Identificado ao abrir a mensagem.
    html = client.get("/email/m1").get_data(as_text=True)
    assert "fatura-42.pdf" in html

    # 2) Descarregado (bytes só agora).
    resposta = client.get("/email/m1/anexo/anexo1")
    assert resposta.status_code == 200
    assert resposta.data == pdf

    # 3) Guardado como documento, com referência à origem.
    resposta = client.post(
        "/email/m1/anexo/anexo1/guardar",
        data={"csrf_token": _csrf(client, "/email/m1")},
        follow_redirects=True,
    )
    assert "documento local" in resposta.get_data(as_text=True)
    linhas = db.rows("SELECT * FROM documentos")
    assert len(linhas) == 1
    assert linhas[0]["origem"] == "anexo"
    assert linhas[0]["gmail_mensagem_id"] == "m1"
    assert linhas[0]["gmail_anexo_id"] == "anexo1"

    # Guardar um anexo nunca cria movimentos financeiros.
    assert db.rows("SELECT * FROM payments") == []


# ------------------------------------------------------- guardar como documento --


def test_guardar_email_como_documento_guarda_copia_local_com_origem(app, client, db, monkeypatch):
    from app.integracoes import guardar_integracao

    with app.app_context():
        for chave, valor in (
            ("gmail_client_id", "c"), ("gmail_client_secret", "s"),
            ("gmail_refresh_token", "r"), ("gmail_access_token", "a"),
            ("gmail_access_token_expira", "9999999999"),
        ):
            guardar_integracao(chave, valor)
    monkeypatch.setattr("app.gmail.pedido_json", _mensagem_com_html("<p>Limpeza de janeiro</p>"))

    resposta = client.post(
        "/email/m1/guardar",
        data={"csrf_token": _csrf(client, "/email/m1")},
        follow_redirects=True,
    )
    html = resposta.get_data(as_text=True)
    assert "documento local" in html

    linhas = db.rows("SELECT * FROM documentos")
    assert len(linhas) == 1
    assert linhas[0]["origem"] == "email"
    assert linhas[0]["gmail_mensagem_id"] == "m1"
    assert linhas[0]["decisao"] == "guardado"

    # A cópia existe no disco, fora da pasta pública, com nome interno.
    from app.documentos import pasta_documentos

    with app.app_context():
        caminho = pasta_documentos() / linhas[0]["nome_interno"]
    assert caminho.exists()
    assert "Limpeza de janeiro" in caminho.read_text(encoding="utf-8")
    assert caminho.name != linhas[0]["nome_original"]
