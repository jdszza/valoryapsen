# -*- coding: utf-8 -*-
"""Placas falsas — uma por subsistema com firmware.

Cada uma fala o contrato de `docs/PROTOCOLO_SERIAL.md` por uma porta que o
`serial_link` abre com `serial.serial_for_url()`, exatamente como abriria a
porta de uma placa de verdade. São o que permite exercitar o transporte inteiro
sem hardware, e são também o documento executável contra o qual os três
firmwares vão ser escritos — o mesmo papel que
`painel_operador/firmware/simulador_serial.py` cumpre para o display de 7".
"""
