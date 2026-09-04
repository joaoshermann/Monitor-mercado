# -*- coding: utf-8 -*-
"""
Monitor de Mercado -> Telegram
================================
Roda a cada 30 minutos (via GitHub Actions) e manda mensagem no Telegram
quando encontra:
  1. Movimento grande (1%+) no Ibovespa ou no dolar/euro
  2. Movimento grande (3%+) em alguma acao da lista de acompanhamento
  3. Noticia nova sobre o mercado em geral (Selic, Copom, Ibovespa, etc.)
  4. Noticia nova sobre alguma empresa da lista de acompanhamento

Nao precisa mexer neste arquivo pra usar - so editar a lista ACOES_ACOMPANHAR
e as PALAVRAS_MERCADO_GERAL abaixo, se quiser trocar o que e monitorado.
"""

import json
import os
import re
import time
import urllib.request
import urllib.error
from datetime import datetime
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

# ============================================================
# CONFIGURACOES - PODE AJUSTAR AQUI
# ============================================================

# Limiar de variacao (%) para o Ibovespa e cambio dispararem alerta.
LIMIAR_INDICE = 1.0

# Limiar de variacao (%) para uma acao individual da lista disparar alerta
# (mais alto que o indice pois acoes individuais oscilam mais no dia a dia).
LIMIAR_ACAO = 3.0

# Acoes que voce quer acompanhar (ticker -> nome usado na busca de noticias).
# Isto NAO e recomendacao de investimento, e so uma lista de monitoramento.
ACOES_ACOMPANHAR = {
    # grandes / blue chips
    "PETR4": "Petrobras",
    "VALE3": "Vale",
    "ITUB4": "Itau Unibanco",
    "BBDC4": "Bradesco",
    "ABEV3": "Ambev",
    "WEGE3": "WEG",
    "BBAS3": "Banco do Brasil",
    "B3SA3": "B3",
    "RENT3": "Localiza",
    "ITSA4": "Itausa",
    # potencial / crescimento
    "PRIO3": "PRIO PetroRio",
    "RAIZ4": "Raizen",
    "ASAI3": "Assai",
    "VAMO3": "Vamos",
    "LWSA3": "Locaweb",
    "TOTS3": "Totvs",
}

# Termos de busca para noticias gerais de mercado (nao ligadas a uma acao
# especifica).
PALAVRAS_MERCADO_GERAL = [
    "Ibovespa hoje",
    "Selic Copom",
    "dolar hoje Brasil",
    "mercado financeiro Brasil",
]

# Noticia so e considerada "nova" se foi publicada ha menos que isso.
NOTICIA_MAX_IDADE_MINUTOS = 45

ARQUIVO_ESTADO = "state/estado.json"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# ============================================================
# UTILITARIOS
# ============================================================


def agora_brasil():
    return datetime.now(ZoneInfo("America/Sao_Paulo"))


def hoje_str():
    return agora_brasil().strftime("%Y-%m-%d")


def carregar_estado():
    if os.path.exists(ARQUIVO_ESTADO):
        try:
            with open(ARQUIVO_ESTADO, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"indices_alertados": {}, "noticias_enviadas": []}


def salvar_estado(estado):
    # mantem so as ultimas 500 noticias na memoria, pra nao crescer pra sempre
    estado["noticias_enviadas"] = estado["noticias_enviadas"][-500:]
    os.makedirs(os.path.dirname(ARQUIVO_ESTADO), exist_ok=True)
    with open(ARQUIVO_ESTADO, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=2)


def enviar_telegram(mensagem):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("AVISO: TELEGRAM_TOKEN ou TELEGRAM_CHAT_ID nao configurados. Mensagem nao enviada:")
        print(mensagem)
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    dados = json.dumps({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": mensagem,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=dados, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
        print("Mensagem enviada:", mensagem[:60].replace("\n", " "), "...")
        return True
    except urllib.error.URLError as e:
        print("ERRO ao enviar Telegram:", e)
        return False


# ============================================================
# COTACOES (Ibovespa, cambio, acoes)
# ============================================================


def obter_variacao_yahoo(ticker_yahoo):
    """Retorna (preco_atual, variacao_percentual) usando a API publica de
    graficos do Yahoo Finance. ticker_yahoo exemplos: '^BVSP', 'PETR4.SA'."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker_yahoo}?interval=1d&range=5d"
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        meta = data["chart"]["result"][0]["meta"]
        preco = meta.get("regularMarketPrice")
        anterior = meta.get("chartPreviousClose") or meta.get("previousClose")
        if preco is None or not anterior:
            return None, None
        variacao = (preco - anterior) / anterior * 100
        return preco, variacao
    except Exception as e:
        print(f"  erro ao consultar {ticker_yahoo}: {e}")
        return None, None


def obter_variacao_cambio():
    """USD-BRL e EUR-BRL via AwesomeAPI (gratuita, sem chave)."""
    url = "https://economia.awesomeapi.com.br/json/last/USD-BRL,EUR-BRL"
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        resultado = {}
        for chave, nome in [("USDBRL", "Dolar"), ("EURBRL", "Euro")]:
            item = data.get(chave)
            if item:
                resultado[nome] = (float(item["bid"]), float(item["pctChange"]))
        return resultado
    except Exception as e:
        print(f"  erro ao consultar cambio: {e}")
        return {}


def checar_indices(estado):
    hoje = hoje_str()
    alertados = estado.setdefault("indices_alertados", {})

    ativos = {}

    preco, var = obter_variacao_yahoo("%5EBVSP")
    if var is not None:
        ativos["Ibovespa"] = (preco, var)

    ativos.update(obter_variacao_cambio())

    for nome, (preco, var) in ativos.items():
        chave = f"{nome}_{hoje}"
        if abs(var) >= LIMIAR_INDICE and chave not in alertados:
            direcao = "📈 subiu" if var > 0 else "📉 caiu"
            msg = (f"<b>{nome}</b> {direcao} <b>{var:+.2f}%</b> hoje\n"
                   f"Valor atual: {preco:,.2f}")
            if enviar_telegram(msg):
                alertados[chave] = True
        time.sleep(1)


def checar_acoes(estado):
    hoje = hoje_str()
    alertados = estado.setdefault("indices_alertados", {})

    for ticker, nome in ACOES_ACOMPANHAR.items():
        preco, var = obter_variacao_yahoo(f"{ticker}.SA")
        if var is None:
            time.sleep(1)
            continue
        chave = f"{ticker}_{hoje}"
        if abs(var) >= LIMIAR_ACAO and chave not in alertados:
            direcao = "📈 subiu" if var > 0 else "📉 caiu"
            msg = (f"<b>{ticker}</b> ({nome}) {direcao} <b>{var:+.2f}%</b> hoje\n"
                   f"Valor atual: R$ {preco:,.2f}")
            if enviar_telegram(msg):
                alertados[chave] = True
        time.sleep(1)


# ============================================================
# NOTICIAS (Google News RSS, gratuito, sem chave)
# ============================================================


def buscar_noticias(termo_busca, max_idade_min=45):
    """Busca noticias recentes no Google News RSS para um termo de busca."""
    termo_url = urllib.request.quote(termo_busca)
    url = f"https://news.google.com/rss/search?q={termo_url}&hl=pt-BR&gl=BR&ceid=BR:pt-419"
    req = urllib.request.Request(url, headers=HEADERS)
    resultado = []
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            xml_bytes = resp.read()
        raiz = ElementTree.fromstring(xml_bytes)
        agora = agora_brasil()
        for item in raiz.findall(".//item"):
            titulo = item.findtext("title") or ""
            link = item.findtext("link") or ""
            pub_data_txt = item.findtext("pubDate") or ""
            try:
                pub_data = datetime.strptime(pub_data_txt, "%a, %d %b %Y %H:%M:%S %Z")
                pub_data = pub_data.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo("America/Sao_Paulo"))
                idade_min = (agora - pub_data).total_seconds() / 60
            except Exception:
                idade_min = 0  # se nao conseguir parsear a data, considera recente
            if idade_min <= max_idade_min:
                resultado.append((titulo, link))
    except Exception as e:
        print(f"  erro ao buscar noticias de '{termo_busca}': {e}")
    return resultado


def checar_noticias_gerais(estado):
    enviadas = estado.setdefault("noticias_enviadas", [])
    for termo in PALAVRAS_MERCADO_GERAL:
        for titulo, link in buscar_noticias(termo, NOTICIA_MAX_IDADE_MINUTOS):
            if link not in enviadas:
                msg = f"📰 <b>Mercado</b>\n{titulo}\n{link}"
                if enviar_telegram(msg):
                    enviadas.append(link)
        time.sleep(1)


def checar_noticias_empresas(estado):
    enviadas = estado.setdefault("noticias_enviadas", [])
    for ticker, nome in ACOES_ACOMPANHAR.items():
        termo = f"{nome} acoes"
        for titulo, link in buscar_noticias(termo, NOTICIA_MAX_IDADE_MINUTOS):
            if link not in enviadas:
                msg = f"📰 <b>{ticker}</b> ({nome})\n{titulo}\n{link}"
                if enviar_telegram(msg):
                    enviadas.append(link)
        time.sleep(1)


# ============================================================
# MAIN
# ============================================================


def main():
    print(f"=== Rodando verificacao as {agora_brasil().strftime('%d/%m/%Y %H:%M:%S')} (Brasilia) ===")
    estado = carregar_estado()

    print("Checando Ibovespa e cambio...")
    checar_indices(estado)

    print("Checando acoes da lista...")
    checar_acoes(estado)

    print("Checando noticias gerais de mercado...")
    checar_noticias_gerais(estado)

    print("Checando noticias das empresas da lista...")
    checar_noticias_empresas(estado)

    salvar_estado(estado)
    print("Concluido.")


if __name__ == "__main__":
    main()
