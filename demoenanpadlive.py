# -*- coding: utf-8 -*-
# demo_enanpad_live.py
# Demonstração ao vivo da sessão do EnANPAD 2026 - BOVA11, ciclo contínuo.
# Local: C:\repo\wpoptionsvol\enanpad26
#
# Calibra duas curvas SVI por ativo, calls e puts, e atualiza os gráficos
# a cada ciclo, sem abrir janelas novas. Sem envio de ordem.
#
# CICLO_CONTINUO = True  -> repete a cada INTERVALO segundos até Ctrl+C
# CICLO_CONTINUO = False -> uma passagem só
#
# Requer o terminal MT5 aberto e conectado, durante o pregão.

import math
import time
import datetime
import numpy as np
import matplotlib.pyplot as plt
from scipy.optimize import least_squares
import MetaTrader5 as mt5

# ----------------------------------------------------------------------
# Parâmetros
# ----------------------------------------------------------------------

ATIVOS = [("BOVA11", "BOVA", "BOVA11")]

CICLO_CONTINUO = True
INTERVALO = 15.0              # segundos entre ciclos

VENCIMENTO_FIXO = "2026-10-16"   # None escolhe o mais próximo com DTE >= DTE_MINIMO
DTE_MINIMO = 21

TAXA = 0.1435                 # CDI aproximado, ao ano
DIAS_UTEIS = 252

FORWARD_IMPLICITO = True      # extrai F da paridade put-call em vez de S0*exp(rT)
BANDA_PARIDADE = 0.05         # |ln(K/F_teorico)| maximo dos pares usados na paridade

MIN_MID = 0.05
MAX_MONEYNESS = 0.15
MAX_MONEYNESS_ITM = 0.08
MAX_SPREAD_REL = 0.50
MIN_IV = 0.02
LIMIAR = 0.02                 # limiar de sinal, em pontos de volatilidade
RMSE_MAXIMO = 0.05            # descarte do ciclo acima de 5 pontos de volatilidade

PAUSA_ASSINATURA = 4.0        # espera após incluir os símbolos no Market Watch
MOSTRAR_GRAFICO = True

plt.rcParams["font.size"] = 11
plt.rcParams["axes.linewidth"] = 0.8

# ----------------------------------------------------------------------
# Conexão
# ----------------------------------------------------------------------

print("")
print("=" * 64)
print("  CALIBRAÇÃO SVI EM TEMPO REAL - OPÇÕES B3")
print("  %s" % datetime.datetime.now().strftime("%d/%m/%Y  %H:%M:%S"))
print("=" * 64)

if not mt5.initialize():
    raise SystemExit("Falha ao inicializar o MT5: %s" % str(mt5.last_error()))

conta = mt5.account_info()
print("")
print("  Servidor ........ %s" % conta.server)
print("  Conta ........... %s  (%s)" % (conta.login, "demo" if conta.trade_mode == 0 else "real"))

todos = mt5.symbols_get()
print("  Símbolos ........ %d no servidor" % len(todos))

simbolos_opcoes = []
for s in todos:
    caminho = getattr(s, "path", "") or ""
    if "OPCOES" in caminho.upper():
        simbolos_opcoes.append(s)
print("  Em BOVESPA\\OPCOES %d" % len(simbolos_opcoes))

if MOSTRAR_GRAFICO:
    plt.ion()

cadeia_por_ativo = {}         # cache da cadeia: descoberta uma vez por ativo
figura_por_ativo = {}         # figura e eixos reutilizados a cada ciclo
ciclo = 0

# ----------------------------------------------------------------------
# Laço de ciclos
# ----------------------------------------------------------------------

try:
    while True:

        ciclo = ciclo + 1
        primeira_vez = (ciclo == 1)
        resumo_final = []

        print("")
        print("=" * 64)
        print("  CICLO %d   %s" % (ciclo, datetime.datetime.now().strftime("%H:%M:%S")))
        print("=" * 64)

        for nome_ativo, raiz, ticker_spot in ATIVOS:

            if primeira_vez:
                print("")
                print("-" * 64)
                print("  %s" % nome_ativo)
                print("-" * 64)

            # ---- preço à vista -------------------------------------------

            mt5.symbol_select(ticker_spot, True)
            tick_spot = mt5.symbol_info_tick(ticker_spot)
            if tick_spot is None:
                print("  [%s] sem cotação do ativo à vista." % nome_ativo)
                continue
            if tick_spot.bid > 0 and tick_spot.ask > 0:
                spot = 0.5 * (tick_spot.bid + tick_spot.ask)
            else:
                spot = tick_spot.last
            if spot is None or spot <= 0:
                print("  [%s] preço à vista inválido." % nome_ativo)
                continue

            # ---- descoberta da cadeia, apenas no primeiro ciclo -----------

            if nome_ativo not in cadeia_por_ativo:

                info_opcoes = []
                for s in simbolos_opcoes:
                    if not s.name.upper().startswith(raiz.upper()):
                        continue
                    info = mt5.symbol_info(s.name)
                    if info is None:
                        continue
                    venc_unix = getattr(info, "expiration_time", 0)
                    if venc_unix is None or venc_unix <= 86400:
                        continue
                    strike = getattr(info, "option_strike", 0.0)
                    if strike is None or strike <= 0:
                        continue
                    try:
                        data_venc = datetime.datetime.fromtimestamp(venc_unix).date()
                    except (OSError, OverflowError, ValueError):
                        continue
                    tipo = "C" if getattr(info, "option_right", 0) == 0 else "P"
                    info_opcoes.append((s.name, strike, data_venc, tipo))

                if len(info_opcoes) == 0:
                    print("  [%s] nenhuma opção encontrada para a raiz %s." % (nome_ativo, raiz))
                    continue

                hoje = datetime.date.today()
                vencimentos = sorted(set([x[2] for x in info_opcoes]))
                venc = None
                if VENCIMENTO_FIXO is not None:
                    alvo = datetime.datetime.strptime(VENCIMENTO_FIXO, "%Y-%m-%d").date()
                    if alvo in vencimentos:
                        venc = alvo
                if venc is None:
                    for v in vencimentos:
                        if (v - hoje).days >= DTE_MINIMO:
                            venc = v
                            break
                if venc is None:
                    print("  [%s] nenhum vencimento utilizável. Disponíveis: %s"
                          % (nome_ativo, ", ".join([v.strftime("%d/%m") for v in vencimentos[:6]])))
                    continue

                cadeia = [x for x in info_opcoes if x[2] == venc]

                # inclui os símbolos no Market Watch e espera a assinatura
                n_sel = 0
                for nome_simbolo, strike, data_venc, tipo in cadeia:
                    if mt5.symbol_select(nome_simbolo, True):
                        n_sel = n_sel + 1
                print("  Assinando %d símbolos de %s no Market Watch (%.0f s)..."
                      % (n_sel, nome_ativo, PAUSA_ASSINATURA))
                time.sleep(PAUSA_ASSINATURA)

                cadeia_por_ativo[nome_ativo] = (cadeia, venc)

            cadeia, venc = cadeia_por_ativo[nome_ativo]
            hoje = datetime.date.today()
            dte = (venc - hoje).days
            T = dte / float(DIAS_UTEIS)
            forward_teorico = spot * math.exp(TAXA * T)
            forward = forward_teorico

            # ---- forward implícito pela paridade put-call ----------------
            # c - p = S0 - K e^{-rT}  =>  F = (c - p) e^{rT} + K

            if FORWARD_IMPLICITO:
                precos_por_strike = {}
                for nome_simbolo, strike, data_venc, tipo in cadeia:
                    tick_p = mt5.symbol_info_tick(nome_simbolo)
                    if tick_p is None:
                        continue
                    if tick_p.bid > 0 and tick_p.ask > 0:
                        preco = 0.5 * (tick_p.bid + tick_p.ask)
                        sp = tick_p.ask - tick_p.bid
                    elif tick_p.last > 0:
                        preco = tick_p.last
                        sp = 0.0
                    else:
                        continue
                    if preco < MIN_MID:
                        continue
                    if abs(math.log(strike / forward_teorico)) > BANDA_PARIDADE:
                        continue
                    if sp > 0 and (sp / preco) > 0.20:
                        continue
                    if strike not in precos_por_strike:
                        precos_por_strike[strike] = {}
                    precos_por_strike[strike][tipo] = preco

                estimativas = []
                for strike in sorted(precos_por_strike.keys()):
                    par = precos_por_strike[strike]
                    if "C" in par and "P" in par:
                        estimativas.append((par["C"] - par["P"]) * math.exp(TAXA * T) + strike)

                if len(estimativas) >= 3:
                    forward = float(np.median(estimativas))

            if primeira_vez:
                print("")
                print("  Spot ............ R$ %.2f" % spot)
                print("  Vencimento ...... %s   (DTE = %d)" % (venc.strftime("%d/%m/%Y"), dte))
                print("  Forward teórico . R$ %.2f" % forward_teorico)
                if FORWARD_IMPLICITO:
                    print("  Forward implícito R$ %.2f   (desvio de %+.2f%% do teórico)"
                          % (forward, 100.0 * (forward / forward_teorico - 1.0)))

            # ---- cascata de filtros e volatilidade implícita --------------

            n_bruto = len(cadeia)
            n_preco = 0
            n_mid = 0
            n_money = 0
            n_itm = 0
            n_spread = 0
            n_iv = 0

            lista_k = []
            lista_strike = []
            lista_tipo = []
            lista_iv = []
            lista_peso = []

            desconto = math.exp(-TAXA * T)

            for nome_simbolo, strike, data_venc, tipo in cadeia:

                tick = mt5.symbol_info_tick(nome_simbolo)
                if tick is None:
                    continue

                bid = tick.bid
                ask = tick.ask
                if bid > 0 and ask > 0:
                    mid = 0.5 * (bid + ask)
                    spread = ask - bid
                elif tick.last > 0:
                    mid = tick.last
                    spread = 0.0
                else:
                    continue
                n_preco = n_preco + 1

                if mid < MIN_MID:
                    continue
                n_mid = n_mid + 1

                k = math.log(strike / forward)
                if abs(k) > MAX_MONEYNESS:
                    continue
                n_money = n_money + 1

                if tipo == "C" and k < -MAX_MONEYNESS_ITM:
                    continue
                if tipo == "P" and k > MAX_MONEYNESS_ITM:
                    continue
                n_itm = n_itm + 1

                if spread > 0 and (spread / mid) > MAX_SPREAD_REL:
                    continue
                n_spread = n_spread + 1

                # bisseção na forma de Black, sobre o forward:
                # call e put do mesmo strike são invertidas com o mesmo insumo
                intrinseco = desconto * (max(forward - strike, 0.0) if tipo == "C"
                                         else max(strike - forward, 0.0))
                if mid <= intrinseco * 0.999:
                    continue
                sigma_lo = 0.001
                sigma_hi = 5.0
                for _ in range(100):
                    sigma_mid = 0.5 * (sigma_lo + sigma_hi)
                    raiz_t = sigma_mid * math.sqrt(T)
                    d1 = (math.log(forward / strike) + 0.5 * raiz_t * raiz_t) / raiz_t
                    d2 = d1 - raiz_t
                    n_d1 = 0.5 * (1.0 + math.erf(d1 / math.sqrt(2.0)))
                    n_d2 = 0.5 * (1.0 + math.erf(d2 / math.sqrt(2.0)))
                    if tipo == "C":
                        preco_modelo = desconto * (forward * n_d1 - strike * n_d2)
                    else:
                        preco_modelo = desconto * (strike * (1.0 - n_d2) - forward * (1.0 - n_d1))
                    if preco_modelo > mid:
                        sigma_hi = sigma_mid
                    else:
                        sigma_lo = sigma_mid
                    if (sigma_hi - sigma_lo) < 1e-8:
                        break
                iv = 0.5 * (sigma_lo + sigma_hi)

                if iv < MIN_IV:
                    continue
                n_iv = n_iv + 1

                lista_k.append(k)
                lista_strike.append(strike)
                lista_tipo.append(tipo)
                lista_iv.append(iv)
                lista_peso.append(mid / spread if spread > 0 else 1.0)

            if primeira_vez:
                print("")
                print("  CASCATA DE FILTROS")
                print("    cadeia bruta ................. %4d" % n_bruto)
                print("    com cotação .................. %4d   (-%d)" % (n_preco, n_bruto - n_preco))
                print("    preço médio >= R$ %.2f ....... %4d   (-%d)" % (MIN_MID, n_mid, n_preco - n_mid))
                print("    |k| <= %.2f .................. %4d   (-%d)" % (MAX_MONEYNESS, n_money, n_mid - n_money))
                print("    ITM assimétrico <= %.2f ...... %4d   (-%d)" % (MAX_MONEYNESS_ITM, n_itm, n_money - n_itm))
                print("    spread relativo <= %.2f ...... %4d   (-%d)" % (MAX_SPREAD_REL, n_spread, n_itm - n_spread))
                print("    VI >= %.2f ................... %4d   (-%d)" % (MIN_IV, n_iv, n_spread - n_iv))

            if n_iv < 10:
                print("  [%s] observações insuficientes para calibrar." % nome_ativo)
                resumo_final.append("  %-8s  sem calibração" % nome_ativo)
                continue

            k_obs = np.array(lista_k)
            strike_obs = np.array(lista_strike)
            tipo_obs = np.array(lista_tipo)
            iv_obs = np.array(lista_iv)
            peso_obs = np.array(lista_peso)
            w_obs = iv_obs * iv_obs * T

            eh_call = (tipo_obs == "C")
            eh_put = (tipo_obs == "P")

            if primeira_vez:
                print("")
                print("  PESOS DE LIQUIDEZ  (inverso do spread relativo)")
                print("    calls: %d observações   |   puts: %d observações"
                      % (int(eh_call.sum()), int(eh_put.sum())))
                print("    peso mínimo %.1f   mediano %.1f   máximo %.1f"
                      % (peso_obs.min(), float(np.median(peso_obs)), peso_obs.max()))

            # ---- calibração de duas curvas -------------------------------

            k_amplitude = float(k_obs.max() - k_obs.min())
            limite_inf = [-np.inf, 1e-6, -0.95, -0.5 * k_amplitude, 0.02]
            limite_sup = [np.inf, 2.0, 0.95, 0.5 * k_amplitude, 1.5 * k_amplitude]

            w_atm = float(np.median(w_obs))
            partidas = [[w_atm * 0.9, 0.10, -0.30, 0.00, 0.20],
                        [w_atm * 0.8, 0.05, -0.30, 0.00, 0.20],
                        [w_atm * 0.8, 0.10, -0.50, 0.00, 0.15],
                        [w_atm * 0.8, 0.15, -0.70, -0.05, 0.10],
                        [w_atm * 0.5, 0.08, -0.40, 0.02, 0.25],
                        [w_atm * 0.8, 0.10, +0.30, 0.00, 0.20]]

            theta_tipo = {}
            rmse_tipo = {}
            borboleta_tipo = {}
            falhou = False

            for tipo_alvo in ("C", "P"):
                sel = (tipo_obs == tipo_alvo)
                if int(sel.sum()) < 5:
                    falhou = True
                    break
                k_s = k_obs[sel]
                w_s = w_obs[sel]
                pesos_s = peso_obs[sel] / peso_obs[sel].sum()
                raiz_s = np.sqrt(pesos_s)
                residuo = lambda p, kk=k_s, ww=w_s, rr=raiz_s: rr * (
                    p[0] + p[1] * (p[2] * (kk - p[3]) + np.sqrt((kk - p[3]) ** 2 + p[4] ** 2)) - ww)

                melhor = None
                theta = None
                for p0 in partidas:
                    try:
                        ajuste = least_squares(residuo, p0, bounds=(limite_inf, limite_sup),
                                               method="trf", xtol=1e-10, ftol=1e-10, max_nfev=5000)
                    except Exception:
                        continue
                    if melhor is None or ajuste.cost < melhor:
                        melhor = ajuste.cost
                        theta = ajuste.x
                if theta is None:
                    falhou = True
                    break

                pa, pb, prho, pm, psig = theta
                w_aj = pa + pb * (prho * (k_s - pm) + np.sqrt((k_s - pm) ** 2 + psig ** 2))
                iv_aj = np.sqrt(np.maximum(w_aj, 1e-12) / T)
                theta_tipo[tipo_alvo] = theta
                rmse_tipo[tipo_alvo] = float(np.sqrt(np.mean((iv_obs[sel] - iv_aj) ** 2)))
                borboleta_tipo[tipo_alvo] = pb * (1.0 + abs(prho))

            if falhou:
                print("  [%s] calibração não concluída." % nome_ativo)
                resumo_final.append("  %-8s  sem calibração" % nome_ativo)
                continue

            limite_borboleta = 4.0 / T
            rmse_pior = max(rmse_tipo["C"], rmse_tipo["P"])
            borboleta_ok = (borboleta_tipo["C"] <= limite_borboleta) and (borboleta_tipo["P"] <= limite_borboleta)

            if primeira_vez:
                print("")
                print("  CALIBRAÇÃO SVI  (duas curvas independentes)")
                print("           %10s %10s %10s %10s %10s %9s"
                      % ("a", "b", "rho", "m", "sigma", "RMSE"))
                for tipo_alvo, rotulo in (("C", "calls"), ("P", "puts ")):
                    pa, pb, prho, pm, psig = theta_tipo[tipo_alvo]
                    print("    %s  %+10.5f %10.5f %+10.4f %+10.5f %10.4f %8.2f%%"
                          % (rotulo, pa, pb, prho, pm, psig, rmse_tipo[tipo_alvo] * 100.0))
                print("")
                print("  DIAGNÓSTICOS")
                print("    RMSE pior lado .......... %.2f p.v.   (limite %.0f p.v.)  %s"
                      % (rmse_pior * 100.0, RMSE_MAXIMO * 100.0,
                         "ok" if rmse_pior <= RMSE_MAXIMO else "FALHOU"))
                print("    borboleta ............... calls %.3f   puts %.3f   (limite 4/T = %.1f)  %s"
                      % (borboleta_tipo["C"], borboleta_tipo["P"], limite_borboleta,
                         "ok" if borboleta_ok else "FALHOU"))

            # ---- resíduos e condição conjunta ----------------------------

            residuos = np.zeros_like(iv_obs)
            for tipo_alvo in ("C", "P"):
                sel = (tipo_obs == tipo_alvo)
                pa, pb, prho, pm, psig = theta_tipo[tipo_alvo]
                w_t = pa + pb * (prho * (k_obs[sel] - pm) + np.sqrt((k_obs[sel] - pm) ** 2 + psig ** 2))
                residuos[sel] = iv_obs[sel] - np.sqrt(np.maximum(w_t, 1e-12) / T)

            strikes_comuns = sorted(set(strike_obs[eh_call].tolist()) & set(strike_obs[eh_put].tolist()))
            sinais = []
            for strike in strikes_comuns:
                i_c = np.where(eh_call & (strike_obs == strike))[0]
                i_p = np.where(eh_put & (strike_obs == strike))[0]
                if len(i_c) == 0 or len(i_p) == 0:
                    continue
                res_c = residuos[i_c[0]]
                res_p = residuos[i_p[0]]
                if res_c > LIMIAR and res_p > LIMIAR:
                    sinais.append((strike, res_c, res_p, "VENDER VOL"))
                elif res_c < -LIMIAR and res_p < -LIMIAR:
                    sinais.append((strike, res_c, res_p, "COMPRAR VOL"))

            if primeira_vez:
                print("")
                print("  CONDIÇÃO CONJUNTA  (limiar de %.0f p.v.)" % (LIMIAR * 100.0))
                print("    strikes com call e put disponíveis: %d" % len(strikes_comuns))
                print("    resíduos acima do limiar: %d calls, %d puts"
                      % (int(np.sum(np.abs(residuos[eh_call]) > LIMIAR)),
                         int(np.sum(np.abs(residuos[eh_put]) > LIMIAR))))

            for strike, res_c, res_p, direcao in sinais:
                print("    [%s] strike %.2f   resíduo call %+.4f   resíduo put %+.4f   -> %s"
                      % (nome_ativo, strike, res_c, res_p, direcao))

            if len(sinais) == 0:
                resumo_final.append("  %-8s  spot %8.2f   RMSE %.2f p.v.   sem sinal"
                                    % (nome_ativo, spot, rmse_pior * 100.0))
            else:
                resumo_final.append("  %-8s  spot %8.2f   RMSE %.2f p.v.   %d sinal(is)"
                                    % (nome_ativo, spot, rmse_pior * 100.0, len(sinais)))

            # ---- gráfico reutilizado a cada ciclo ------------------------

            if MOSTRAR_GRAFICO:

                if nome_ativo not in figura_por_ativo:
                    fig_a, eixos_a = plt.subplots(2, 1, figsize=(9.5, 6.0), sharex=True,
                                                  gridspec_kw={"height_ratios": [2.2, 1.0],
                                                               "hspace": 0.12},
                                                  num="SVI - %s" % nome_ativo)
                    fig_a.subplots_adjust(left=0.10, right=0.97, top=0.92, bottom=0.10)
                    figura_por_ativo[nome_ativo] = (fig_a, eixos_a[0], eixos_a[1])

                fig_a, ax1, ax2 = figura_por_ativo[nome_ativo]
                ax1.clear()
                ax2.clear()

                for tipo_alvo, cor, estilo, rotulo in (("C", "black", "-", "SVI calls"),
                                                       ("P", "0.45", "--", "SVI puts")):
                    sel = (tipo_obs == tipo_alvo)
                    k_t = np.linspace(k_obs[sel].min(), k_obs[sel].max(), 300)
                    pa, pb, prho, pm, psig = theta_tipo[tipo_alvo]
                    w_t = pa + pb * (prho * (k_t - pm) + np.sqrt((k_t - pm) ** 2 + psig ** 2))
                    ax1.plot(k_t, np.sqrt(np.maximum(w_t, 1e-12) / T) * 100.0,
                             color=cor, linestyle=estilo, linewidth=1.3, label=rotulo)

                ax1.scatter(k_obs[eh_call], iv_obs[eh_call] * 100.0, s=28, facecolors="none",
                            edgecolors="black", linewidths=0.9, label="Calls")
                ax1.scatter(k_obs[eh_put], iv_obs[eh_put] * 100.0, s=28, marker="x",
                            color="black", linewidths=0.9, label="Puts")
                ax1.set_ylabel("Volatilidade implícita (% a.a.)")
                ax1.set_title("%s   ciclo %d   %s   spot R$ %.2f   venc. %s   DTE %d   n = %d   RMSE %.2f p.v."
                              % (nome_ativo, ciclo, datetime.datetime.now().strftime("%H:%M:%S"),
                                 spot, venc.strftime("%d/%m/%Y"), dte, n_iv, rmse_pior * 100.0),
                              fontsize=10, loc="left")
                ax1.legend(frameon=False, fontsize=9, ncol=4, loc="lower left")
                ax1.spines["top"].set_visible(False)
                ax1.spines["right"].set_visible(False)

                ax2.axhline(0.0, color="black", linewidth=0.8)
                ax2.axhline(LIMIAR * 100.0, color="0.5", linestyle="--", linewidth=0.8)
                ax2.axhline(-LIMIAR * 100.0, color="0.5", linestyle="--", linewidth=0.8)
                ax2.scatter(k_obs[eh_call], residuos[eh_call] * 100.0, s=26, facecolors="none",
                            edgecolors="black", linewidths=0.9)
                ax2.scatter(k_obs[eh_put], residuos[eh_put] * 100.0, s=26, marker="x",
                            color="black", linewidths=0.9)
                for strike, res_c, res_p, direcao in sinais:
                    k_sinal = math.log(strike / forward)
                    ax2.plot(k_sinal, res_c * 100.0, marker="o", markerfacecolor="none",
                             markeredgecolor="black", markersize=16, markeredgewidth=1.1)
                    ax2.plot(k_sinal, res_p * 100.0, marker="o", markerfacecolor="none",
                             markeredgecolor="black", markersize=16, markeredgewidth=1.1)
                ax2.set_xlabel("Log-moneyness  k = ln(K/F)")
                ax2.set_ylabel("Resíduo (p.v.)")
                ax2.spines["top"].set_visible(False)
                ax2.spines["right"].set_visible(False)

                fig_a.canvas.draw_idle()
                plt.pause(0.2)

        # ---- resumo do ciclo ---------------------------------------------

        print("")
        print("  RESUMO DO CICLO %d   %s" % (ciclo, datetime.datetime.now().strftime("%H:%M:%S")))
        for linha in resumo_final:
            print(linha)

        if not CICLO_CONTINUO:
            break

        print("")
        print("  Próximo ciclo em %.0f s. Ctrl+C para encerrar." % INTERVALO)
        if MOSTRAR_GRAFICO:
            plt.pause(INTERVALO)
        else:
            time.sleep(INTERVALO)

except KeyboardInterrupt:
    print("")
    print("  Encerrado pelo operador após %d ciclo(s)." % ciclo)

# ----------------------------------------------------------------------
# Encerramento
# ----------------------------------------------------------------------

print("")
print("  Conteúdo para fins de pesquisa e ensino. Não é recomendação de investimento.")
print("")

mt5.shutdown()

if MOSTRAR_GRAFICO:
    plt.ioff()
    if len(figura_por_ativo) > 0:
        print("  Feche as janelas dos gráficos para encerrar.")
        plt.show()
