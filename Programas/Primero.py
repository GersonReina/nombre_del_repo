import pyautogui
import pyperclip
import yfinance
import webbrowser
from time import sleep

ticker = input("Ingrese el ticker de la acción: ")
datos = yfinance.Ticker(ticker)
tabla = datos.history("1mo")

cierre = tabla["Close"]

maxima = round(cierre.max())
minima = round(cierre.min())
valor_medio = round(cierre.mean())


destinatario = "gerson10@live.com.ar"
asunto = "Prueba de correo"
mensaje = f"Hola, esto es una prueba de envío de correo con datos de los valores máximos: {maxima}, mínimos: {minima} y promedio: {valor_medio} del ticker {ticker}"

webbrowser.open("https://mail.google.com/mail/u/0/#inbox")
sleep(5)


pyautogui.click(x=162, y=219)

sleep(2)
pyperclip.copy(destinatario)
pyautogui.hotkey("command", "v")
pyautogui.press("tab")
sleep(1)
pyperclip.copy(asunto)
pyautogui.hotkey("command", "v")
pyautogui.press("tab")

sleep(1)
pyperclip.copy(mensaje)
pyautogui.hotkey("command", "v")
pyautogui.press("tab")

