import re
import time
from datetime import datetime, date, timedelta
import json
import requests
from redshift_utils import consultar_planes_redshift, consultar_invitados_redshift, consultar_incapacidades_redshift, consultar_referidos_redshift
from secret import obtener_secret, obtener_token_dinamico
from prompts import get_prompt_no_info, get_prompt_info
# --------------------- #
# FUNCIONES AUXILIARES  #
# --------------------- #

#############
# Respuesta #
#############

def responder(mensaje, session_attributes, intent_name, fulfillment_state="Fulfilled",slot_to_elicit=None):
    print("📤 Enviando respuesta a Lex:", mensaje)
    response = {
        "sessionState": {
            "dialogAction": {
                "type": "ElicitSlot" if slot_to_elicit else "ElicitIntent"
            },
            "intent": {
                "name": intent_name,
                "state": fulfillment_state
            },
            "sessionAttributes": session_attributes
        },
        "messages": [
            {
                "contentType": "PlainText",
                "content": mensaje
            }
        ]
    }
    if slot_to_elicit:
        response["sessionState"]["dialogAction"]["slotToElicit"] = slot_to_elicit
    return response

#######################
# Cerrar Conversacion #
#######################

def cerrar_conversacion(mensaje, session_attributes=None):
    if session_attributes is None:
        session_attributes = {}
    session_attributes["esperando_calificacion"] = "true"
    return {
        "sessionState": {
            "dialogAction": {"type": "ElicitIntent"},
            "sessionAttributes": session_attributes
        },
        "messages": [
            {
                "contentType": "PlainText",
                "content": (
                            "¡Gracias por usar nuestro servicio! 🌟\n\n"
                            "¿Podrías calificar tu experiencia?\n\n"
                            "⭐ 1 estrella - Muy mala\n"
                            "⭐⭐ 2 estrellas - Mala\n"
                            "⭐⭐⭐ 3 estrellas - Regular\n"
                            "⭐⭐⭐⭐ 4 estrellas - Buena\n"
                            "⭐⭐⭐⭐⭐ 5 estrellas - Excelente\n\n"
                            "💬 **Responde con un número del 1 al 5:**"
                        )
            }
        ],
    }

#######################
# Terminar sin calificación #
#######################

def terminar_sin_calificacion(mensaje, session_attributes=None):
    """Termina la conversación sin calificar cuando se rechazan políticas"""
    if session_attributes is None:
        session_attributes = {}
    
    return {
        "sessionState": {
            "dialogAction": {"type": "Close"},
            "intent": {
                "name": "SaludoHabeasData", 
                "state": "Fulfilled"
            },
            "sessionAttributes": session_attributes
        },
        "messages": [{
            "contentType": "PlainText",
            "content": mensaje
        }]
    }

############################
# Sugerencias de intension #
############################

def mostrar_sugerencias(session_attributes):
    sugerencias = (
        "Lo siento, no logré identificar tu solicitud 🤔.\n"
        "Pero puedo ayudarte con:\n"
        "📄 Preguntas frecuentes\n"
        "🏢 Sedes disponibles, horarios y actividades\n"
        "📅 Información sobre tu plan\n\n"
        "👥 Consultar invitados\n"
        "🏆 Información sobre referidos\n"
        "🧾 Consultar incapacidades\n"
        "🛍️ Información de ventas\n"
        "❄️ Consultar congelaciones\n\n"
        "¿Sobre cuál tema necesitas ayuda?"
    )
    return responder(sugerencias, session_attributes, "FallbackIntent")


#####################
# Resumen de Planes #
#####################

def resumen_planes_para_bedrock(planes):
    if not planes:
        return "No hay planes activos para este usuario."
    if isinstance(planes, dict):
        planes = [planes]
    if not isinstance(planes, list):
        return "Error: formato de planes no reconocido."
    nombre = planes[0].get("full_name", "Usuario")
    resumen = f"Nombre: {nombre}\nPlanes:\n"
    for plan in planes:
        resumen += (
            f"• Tipo de plan: {plan.get('product_name', 'N/A')}\n"
            f"  Estado: {'Activo ✅' if plan.get('line_status', 0) == 1 else 'Inactivo'}\n"
            f"  Inicio: {plan.get('date_start', 'N/A')}\n"
            f"  Vencimiento: {plan.get('date_end', 'N/A')}\n"
            f"  Sede: {plan.get('venue_use', 'N/A')}\n"
            f"  Recurrente: {'Sí' if plan.get('is_recurring') else 'No'}\n"
            f"  Mora: {plan.get('mora', 'N/A')}\n"
            f"  Detalles: {json.dumps(plan, indent=2)}\n"
        )
    return resumen

##############################
# Convertir Fechas a Strings #
##############################

def convertir_fechas_a_str(obj):
    if isinstance(obj, dict):
        return {k: convertir_fechas_a_str(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [convertir_fechas_a_str(i) for i in obj]
    elif hasattr(obj, 'isoformat'):
        return obj.isoformat()
    else:
        return obj

##################################
# Resumen Informacion Plan       # 
##################################

def obtener_resumen_plan(datos_plan: dict) -> str:
    try:
        data = datos_plan.get("data", {})
        nombre = f"{data.get('name', '')} {data.get('last_name', '')}".strip()
        planes = data.get("plans", [])
        resumen = f"Nombre: {nombre}\n"
        if not planes:
            return resumen + "No hay planes activos."
        for plan in planes:
            resumen += (
                f"\nTipo de plan: {plan.get('product_name')}\n"
                f"Estado: {'Activo ✅' if plan.get('line_status', 0) == 1 else 'Inactivo'}\n"
                f"Inicio: {plan.get('date_start')}\n"
                f"Vencimiento: {plan.get('date_end')}\n"
                f"proxima fecha de pago o corte: {plan.get('fecha_corte', 'N/A')}\n"
                f"Recurrente: {'Sí' if plan.get('is_recurring') else 'No es recurrente'}\n"
                f"Categoría: {plan.get('categoria', 'Sin categoria')}\n"
                f"Mora: {plan.get('mora', 'No se encuentra en mora')}\n"
                "----------------------"
            )
        return resumen.strip()
    except Exception as e:
        print("❌ Error generando resumen:", str(e))
        return "Información no disponible"

###############################
# Consulta Plan               # 
###############################

def consultar_plan(document_type, document_number):
    print("📞 Iniciando consulta de plan...")

    try:
        datos = consultar_planes_redshift(document_type, document_number)
        print("🔎 Datos recibidos de Redshift:", datos)

        # Validación reforzada
        if not datos or not isinstance(datos, list) or len(datos) == 0:
            print("❌ No se encontraron datos del plan en Redshift")
            return None, "No encontramos información del plan asociada a ese documento. Verifica los datos o intenta más tarde."
        
        # Se extrae nombre y apellido del primer plan si existen
        primer_plan = datos[0] if datos else {}
        nombre = primer_plan.get("full_name", "").split()
        name = nombre[0] if nombre else ""
        last_name = " ".join(nombre[1:]) if len(nombre) > 1 else ""

        datos_formateados = {
            "data": {
                "name": name,
                "last_name": last_name,
                "plans": datos
            }
        }

        print("✅ Datos del plan formateados correctamente")
        return datos_formateados, None
    except Exception as e:
        print("❌ Error al consultar el plan en Redshift:", str(e))
        return None, "Lo siento, hubo un problema consultando el plan. Intenta más tarde."

###############################
# Consulta Plan es Recurrente # 
###############################

def obtener_is_recurring_desde_json(session_attributes):
    try:
        datos_json = session_attributes.get("datos_plan_json") or session_attributes.get("info_plan")
        if not datos_json:
            return None
        
        parsed = json.loads(datos_json)
        plans = parsed.get("data", {}).get("plans", [])
        if not plans:
            return None
        
        return plans[0].get("is_recurring", None)
    except Exception as e:
        print("❌ Error al extraer is_recurring:", str(e))
        return None

###############################
# Consulta Plan API # 
###############################

def consultar_plan_api(document_type, document_number):
    print("📞 Iniciando consulta de plan...")

    try:
        config = obtener_secret("main/LexAgenteVirtualSAC")
        api_url = config.get("API_URL_INFO_PLAN")
        access_token = obtener_token_dinamico(config)

        if not access_token:
            print("❌ ACCESS_TOKEN no encontrado en el secret.")
            return None, "Lo siento, hubo un problema autenticando tu solicitud. Intenta más tarde."

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}"
        }
        payload = {
            "document_type": str(document_type),
            "document_number": document_number
        }

        response = requests.post(api_url, json=payload, headers=headers, timeout=20)

        if response.status_code != 200:
            print(f"❌ Error en la llamada al API: {response.status_code} - {response.text}")
            return None, "No encontramos información del plan asociada a ese documento. Verifica los datos o intenta más tarde."

        datos = response.json()
        print("✅ Consulta de plan exitosa:", datos)
        return datos, None

    except Exception as e:
        print("❌ Error al consultar el plan:", str(e))
        return None, "Lo siento, hubo un problema consultando el plan. Intenta más tarde."

###############################
# validar Fecha Valida 
###############################

def es_fecha_valida(fecha):
    # Valida formato YYYY-MM-DD
    return bool(re.match(r"^\d{4}-\d{2}-\d{2}$", fecha))


###############################
# Obtener resumen de clases grupales
###############################

def obtener_resumen_grupales(sede, clase, fecha, horarios):
    horarios_str = "\n".join(
        f"- {h['hora_inicio']} a {h['hora_fin']}" for h in horarios
    )
    return (
        f"Sede: {sede}\n"
        f"Clase: {clase}\n"
        f"Fecha: {fecha}\n"
        f"Horarios:\n{horarios_str}"
    )

###############################
# Resumen de invitados para Bedrock
###############################
def resumen_invitados_para_bedrock(datos_invitados: dict) -> str:
    invitados = datos_invitados.get("data", [])
    resumen = ""
    if not invitados:
        return (f"No tienes invitados registrados este año. Te indico cómo invitar a alguien:"
                  "Cualquier AFILIADO podrá invitar por una (1) sola vez a una (1) persona que deberá reunir las siguientes condiciones: (i) no estar afiliada, (ii) que su estado de afiliación sea inactivo como mínimo en los últimos tres (3) meses, (iii) que no tenga deudas pendientes con el Club Médico Deportivo BODYTECH, y (iv) que no haya disfrutado ninguna otra cortesía en ese período. Con la autorización previa del Gerente de la respectiva sede del Club Médico Deportivo BODYTECH, un AFILIADO podrá invitar hasta tres (3) personas en forma simultánea. Los invitados deberán presentar documento de identidad, firmar el Contrato de Invitado, llenar el documento de Clasificación de población, atender las indicaciones del instructor durante su práctica y respetar las normas y reglamentos del Club Médico Deportivo BODYTECH. El Club Médico Deportivo BODYTECH se reserva el derecho de admisión de aquellos invitados cuya presentación o condiciones personales al momento de solicitar el ingreso no estén acordes con la imagen pública del Club Médico Deportivo BODYTECH, tales como el uso de jean u otra clase de pantalón no apto para la realización de la práctica deportiva, así como zapatos no apropiados. Las políticas de admisión de Invitados están sujetas a cambios, sin previo aviso.")

    for inv in invitados:
        resumen += (
            "Tus invitados registrados este año son:\n"
            f"• Nombre: {inv.get('nombre_invitado')}, "
            f"Documento: {inv.get('document_invitado')}, "
            f"Franquicia: {inv.get('franquicia')}, "
            f"Fecha de atención: {inv.get('fecha_de_atencion')}\n"
        )
    return resumen.strip()
###############################
# Consultar invitados
###############################

def consultar_invitados(document_type, document_number):
    try:
        invitados = consultar_invitados_redshift(document_type, document_number)
        if not invitados:
            return None, "No se encontraron invitados asociados a este documento este año. si deseas invitar a alguien estos son los pasos: \n"
        return {"data": invitados}, None
    except Exception as e:
        print("❌ Error al consultar invitados:", str(e))
        return None, "Ocurrió un error consultando los invitados."
###############################
# Resumen de incapacidades para Bedrock
###############################

def resumen_incapacidades_para_bedrock(datos_incapacidades: dict) -> str:
    incapacidades = datos_incapacidades.get("data", [])
    if not incapacidades:
        return get_prompt_no_info("ConsultaIncapacidades", "")
    resumen = "Tus incapacidades activas son:\n"
    for inc in incapacidades:
        resumen += (
            f"• Nombre: {inc.get('full_name')} ({inc.get('document_number')})\n"
            f"  Incapacidad: {inc.get('name')}\n"
            f"  Estado: {inc.get('status_issues')}\n"
            f"  Desde: {inc.get('date_start')} hasta {inc.get('date_end')}\n"
        )
    return resumen.strip()

###############################
# Consultar Incapacidades
###############################

def consultar_incapacidades(document_type, document_number):
    try:
        incapacidades = consultar_incapacidades_redshift(document_type, document_number)
        if not incapacidades:
            return None, "No tienes incapacidades activas registradas. Si necesitas reportar una incapacidad, comunícate con tu sede o envía el soporte médico a través de la app Bodytech."
        return {"data": incapacidades}, None
    except Exception as e:
        print("❌ Error al consultar incapacidades:", str(e))
        return None, "Ocurrió un error consultando las incapacidades."

###############################
# Resumen Referidos para Bedrock
###############################

def resumen_referidos_para_bedrock(datos_referidos: dict) -> str:
    referidos = datos_referidos.get("data", [])
    if not referidos:
        return ""  # Si no hay referidos, deja el resumen vacío para que Bedrock use la KB
    resumen = "Tus referidos registrados este año son:\n"
    for ref in referidos:
        resumen += (
            f"• Nombre: {ref.get('name')} | Franquicia: {ref.get('franquicia')} | Fecha: {ref.get('fecha')} | Estado: {ref.get('status_plan')}\n"
        )
    return resumen.strip()

###############################
# Consultar Referidos
###############################

def consultar_referidos(document_type, document_number):
    try:
        referidos = consultar_referidos_redshift(document_type, document_number)
        return {"data": referidos}, None
    except Exception as e:
        print("❌ Error al consultar referidos:", str(e))
        return None, "Ocurrió un error consultando los referidos."

###############################
# Resumen Ingresos para Bedrock
###############################

def resumen_ingresos_para_bedrock(datos):
    linea = datos.get("linea", "N/A")
    tipo = datos.get("tipo", "N/A")
    fecha_inicio = datos.get("fecha_inicio", "N/A")
    fecha_fin = datos.get("fecha_fin", "N/A")
    sede = datos.get("sede", "toda la compañía" if tipo == "Total compañía" else "N/A")
    ingresos = datos.get("ingresos", "N/A")
    if tipo == "Por sede":
        sede_str = f"Sede: {sede}"
    else:
        sede_str = "Total compañía"
    return (
        f"Línea: {linea}\n"
        f"{sede_str}\n"
        f"Fechas: {fecha_inicio} a {fecha_fin}\n"
        f"Ingresos: ${ingresos:,}"
    )

###############################
# Normalizar Fechas
############################### 

def normalizar_fecha(fecha_input):
    """Normaliza diferentes formatos de fecha a YYYY-MM-DD"""
    
    if not fecha_input:
        return None, "Por favor, indica una fecha válida."
    
    fecha_input = str(fecha_input).strip().lower()
    print(f"🔍 Normalizando fecha: '{fecha_input}'")
    
    try:
        año_actual = datetime.now().year
        fecha_hoy = date.today()
        
        # Casos especiales mejorados
        if fecha_input in ["hoy", "today"]:
            fecha_normalizada = fecha_hoy.strftime("%Y-%m-%d")
            print(f"✅ Fecha 'hoy': {fecha_normalizada}")
            return fecha_normalizada, None
            
        elif fecha_input in ["mañana", "manana", "tomorrow"]:
            fecha_mañana = fecha_hoy + timedelta(days=1)
            fecha_normalizada = fecha_mañana.strftime("%Y-%m-%d")
            print(f"✅ Fecha 'mañana': {fecha_normalizada}")
            return fecha_normalizada, None
            
        elif fecha_input in ["ayer", "yesterday"]:
            fecha_ayer = fecha_hoy - timedelta(days=1)
            fecha_normalizada = fecha_ayer.strftime("%Y-%m-%d")
            print(f"✅ Fecha 'ayer': {fecha_normalizada}")
            return fecha_normalizada, None

        # Formato "DD de MMMM" o "DD de MMMM de YYYY"
        patron_fecha_natural = r"(\d{1,2})\s+de\s+(\w+)(?:\s+de\s+(\d{4}))?"
        match_natural = re.search(patron_fecha_natural, fecha_input)
        
        if match_natural:
            dia = int(match_natural.group(1))
            mes_texto = match_natural.group(2).lower()
            año = int(match_natural.group(3)) if match_natural.group(3) else año_actual
            
            # Mapeo de meses en español
            meses_map = {
                "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
                "julio": 7, "agosto": 8, "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
                "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
                "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12
            }
            
            mes = meses_map.get(mes_texto)
            if mes:
                fecha_normalizada = f"{año}-{mes:02d}-{dia:02d}"
                # Validar fecha
                datetime.strptime(fecha_normalizada, "%Y-%m-%d")
                print(f"✅ Formato DD de MMMM: {fecha_normalizada}")
                return fecha_normalizada, None
            else:
                return None, f"No reconozco el mes '{mes_texto}'. Usa enero, febrero, marzo, etc."

        # : Formato con barras más flexible
        if "/" in fecha_input:
            partes = [int(p) for p in fecha_input.split("/")]
            
            if len(partes) == 3:
                p1, p2, p3 = partes
                
                # Si el primer número es > 1900, es YYYY/MM/DD
                if p1 > 1900:
                    fecha_normalizada = f"{p1}-{p2:02d}-{p3:02d}"
                # Si el tercer número es > 1900, podría ser DD/MM/YYYY o MM/DD/YYYY
                elif p3 > 1900:
                    # Si p1 > 12, definitivamente es DD/MM/YYYY
                    if p1 > 12:
                        fecha_normalizada = f"{p3}-{p2:02d}-{p1:02d}"
                    # Si p2 > 12, definitivamente es MM/DD/YYYY  
                    elif p2 > 12:
                        fecha_normalizada = f"{p3}-{p1:02d}-{p2:02d}"
                    # Ambiguo - asumimos DD/MM/YYYY (formato colombiano)
                    else:
                        fecha_normalizada = f"{p3}-{p2:02d}-{p1:02d}"
                else:
                    # Formato de 2 dígitos de año - asumimos 20XX
                    año_completo = 2000 + p3 if p3 < 50 else 1900 + p3
                    if p1 > 12:
                        fecha_normalizada = f"{año_completo}-{p2:02d}-{p1:02d}"
                    else:
                        fecha_normalizada = f"{año_completo}-{p2:02d}-{p1:02d}"
                
                # Validar fecha
                datetime.strptime(fecha_normalizada, "%Y-%m-%d")
                print(f"✅ Formato con barras: {fecha_normalizada}")
                return fecha_normalizada, None
            
            # DD/MM (asume año actual)
            elif len(partes) == 2:
                p1, p2 = partes
                if p1 > 12:
                    fecha_normalizada = f"{año_actual}-{p2:02d}-{p1:02d}"
                else:
                    fecha_normalizada = f"{año_actual}-{p2:02d}-{p1:02d}"
                
                datetime.strptime(fecha_normalizada, "%Y-%m-%d")
                print(f"✅ Formato DD/MM: {fecha_normalizada}")
                return fecha_normalizada, None

        #  Formato solo números más flexible (DDMMYYYY, DDMM)
        if re.match(r"^\d+$", fecha_input):
            if len(fecha_input) == 8:  # DDMMYYYY o YYYYMMDD
                if fecha_input[:2] <= "31":  # Probablemente DDMMYYYY
                    dia = fecha_input[:2]
                    mes = fecha_input[2:4]
                    año = fecha_input[4:8]
                else:  # Probablemente YYYYMMDD
                    año = fecha_input[:4]
                    mes = fecha_input[4:6]
                    dia = fecha_input[6:8]
                fecha_normalizada = f"{año}-{mes}-{dia}"
            elif len(fecha_input) == 4:  # DDMM
                dia = fecha_input[:2]
                mes = fecha_input[2:4]
                fecha_normalizada = f"{año_actual}-{mes}-{dia}"
            elif len(fecha_input) == 3:  # DMM o DDM
                if int(fecha_input[0]) <= 3:  # DMM
                    dia = f"0{fecha_input[0]}"
                    mes = fecha_input[1:3]
                else:  # DDM
                    dia = fecha_input[:2]
                    mes = f"0{fecha_input[2]}"
                fecha_normalizada = f"{año_actual}-{mes}-{dia}"
            elif len(fecha_input) == 2:  # DD (asume mes actual)
                mes_actual = fecha_hoy.month
                fecha_normalizada = f"{año_actual}-{mes_actual:02d}-{fecha_input}"
            else:
                return None, "Formato de fecha no válido."
            
            # Validar fecha
            datetime.strptime(fecha_normalizada, "%Y-%m-%d")
            print(f"✅ Formato numérico: {fecha_normalizada}")
            return fecha_normalizada, None

        # Formato YYYY-MM-DD (ya correcto)
        if re.match(r"^\d{4}-\d{2}-\d{2}$", fecha_input):
            datetime.strptime(fecha_input, "%Y-%m-%d")
            print(f"✅ Formato ISO: {fecha_input}")
            return fecha_input, None

        # Si no se pudo procesar
        return None, (
            "Formato de fecha no reconocido. Puedes usar:\n"
            "• YYYY-MM-DD (2025-07-07)\n"
            "• DD/MM/YYYY (07/07/2025)\n"
            "• DD/MM (07/07)\n"
            "• DD de MMMM (7 de julio)\n"
            "• DD de MMMM de YYYY (7 de julio de 2025)\n"
            "• 'hoy' o 'mañana'"
        )
        
    except ValueError as e:
        print(f"❌ Error de validación: {str(e)}")
        return None, "La fecha indicada no es válida. Verifica el día y mes."
    except Exception as e:
        print(f"❌ Error procesando fecha: {str(e)}")
        return None, "Error procesando la fecha. Intenta con un formato diferente."

###############################
# Manjear Respuestas de informacion Adicional
###############################

def manejar_respuestas_info_adicional(session_attributes, input_transcript):
    from services import consultar_kb_bedrock
    from respuestas import consultar_bedrock_generacion
    
    mapeo_info = {
        "esperando_info_incapacidad": "ConsultaIncapacidades",
        "esperando_info_referidos": "FQAReferidos", 
        "esperando_info_invitados": "ConsultarInvitados",
        "esperando_info_sedes": "ConsultarSedes"
    }
    
    # Buscar qué tipo de información está esperando
    for attr_key, intent_name in mapeo_info.items():
        if session_attributes.get(attr_key) == "true":
            session_attributes.pop(attr_key, None)
            
            #  LIMPIAR EL FLUJO ACTIVO INMEDIATAMENTE
            session_attributes.pop("en_flujo_activo", None)
            
            #  CORRECCIÓN: Detectar respuestas afirmativas sin clasificación externa
            if any(palabra in input_transcript.strip().lower() for palabra in ["sí", "si", "quiero más info", "quiero más información", "si, quiero más info", "si, quiero más información"]):
                print(f"🔍 Usuario pidió más información para: {intent_name}")
                
                # Manejo específico para cada intención
                if intent_name == "FQAReferidos":
                    config = obtener_secret("main/LexAgenteVirtualSAC")
                    prompt = get_prompt_info(intent_name, input_transcript)
                    respuesta_kb = consultar_kb_bedrock(prompt, config["BEDROCK_KB_ID_FQAReferidos"])
                    mensaje_final = respuesta_kb.strip()
                elif intent_name == "ConsultaIncapacidades":
                    # Para incapacidades, usar generación directa con prompt específico
                    prompt = get_prompt_info(intent_name, "")
                    mensaje_final = consultar_bedrock_generacion(prompt)
                elif intent_name == "ConsultarInvitados":
                    # Para invitados, usar generación directa con prompt específico
                    prompt = get_prompt_info(intent_name, "")
                    mensaje_final = consultar_bedrock_generacion(prompt)
                else:
                    # Para otras intenciones, generación directa
                    prompt = get_prompt_info(intent_name, "")
                    mensaje_final = consultar_bedrock_generacion(prompt)
                
                return responder_con_pregunta_final(mensaje_final, session_attributes, intent_name)
            
            elif any(palabra in input_transcript.strip().lower() for palabra in ["no", "no gracias", "no, gracias"]):
                print(f"🔍 Usuario NO quiere más información para: {intent_name}")
                return responder_con_pregunta_final("¡Perfecto!", session_attributes, intent_name)
            
            elif intent_name == "ConsultarSedes":
                    # Para sedes, usar generación directa con prompt específico
                    prompt = get_prompt_info(intent_name, "")
                    mensaje_final = consultar_bedrock_generacion(prompt)
            
            else:
                print(f"🔍 Respuesta ambigua para información de {intent_name}: '{input_transcript}'")
                # MANTENER EL FLUJO ACTIVO SOLO SI LA RESPUESTA ES AMBIGUA
                session_attributes["en_flujo_activo"] = intent_name
                session_attributes[attr_key] = "true"  # Volver a marcar esperando info
                
                return {
                    "sessionState": {
                        "dialogAction": {"type": "ElicitIntent"},
                        "sessionAttributes": session_attributes
                    },
                    "messages": [{
                        "contentType": "PlainText",
                        "content": "No entendí tu respuesta. ¿Deseas más información sobre este tema?\n\nResponde: 'Sí' o 'No'"
                    }]
                }
    
    return None
        
###############################
# Responder con pregunta Grupales
###############################

def esperando_respuesta_grupales(session_attributes, input_transcript, slots, intent):
    """
    Maneja toda la lógica de transiciones para ConsultaGrupales
    """
    # Solo procesar si realmente está esperando transición Y hay flujo activo
    if (session_attributes.get("esperando_transicion_grupales") != "true" or 
        not session_attributes.get("en_flujo_activo")):
        return None
    
    input_lower = input_transcript.lower().strip()
    print(f"🔍 Analizando transición grupales: '{input_lower}'")
    
    tipo_transicion_slot = slots.get("tipo_transicion", {}).get("value", {}).get("interpretedValue") if slots.get("tipo_transicion") else None

    # Prioridad: slot > input
    valor = tipo_transicion_slot or input_lower
    
    if input_lower in ["m", "menu", "menú", "menu principal", "menú principal"]:
        print("🔄 Usuario pidió ir al menú principal")
        # Limpiar sesión y redirigir a menú principal (puedes reutilizar tu lógica de menú principal aquí)
        acepto_politicas = session_attributes.get("acepto_politicas")
        documento_attrs = {
            "document_type_id": session_attributes.get("document_type_id"),
            "document_type_raw": session_attributes.get("document_type_raw"),
            "document_number": session_attributes.get("document_number"),
            "intenciones_con_documento": session_attributes.get("intenciones_con_documento")
        }
        session_attributes.clear()
        for k, v in documento_attrs.items():
            if v is not None:
                session_attributes[k] = v
        if acepto_politicas == "true":
            session_attributes["acepto_politicas"] = "true"
        return {
            "sessionState": {
                "dialogAction": {"type": "ElicitIntent"},
                "sessionAttributes": session_attributes
            },
            "messages": [{
                "contentType": "PlainText",
                "content": (
                    "Has regresado al menú principal. ¿En qué puedo ayudarte?\n\n"
                    "Algunas opciones:\n"
                    "📄 Preguntas frecuentes sobre Bodytech\n"
                    "🏢 Consultar sedes y horarios\n"
                    "🏃‍♂️ Clases grupales disponibles\n"
                    "📅 Información de tu plan\n"
                    "👥 Consultar invitados\n"
                    "🏆 Información sobre referidos\n"
                    "🧾 Consultar incapacidades\n"
                    "🛍️ Información de ventas\n"
                    "❄️ Consultar congelaciones\n"
                    "¿Sobre qué tema te gustaría que te ayude?"
                )
            }]
        }
    # Detectar transiciones válidas
    elif valor == "1":
        print("✅ Transición detectada: OTRA CIUDAD")
        return _procesar_otra_ciudad(session_attributes)
    elif valor == "2":
        print("✅ Transición detectada: OTRA SEDE")
        return _procesar_otra_sede(session_attributes)
    elif valor == "3":
        print("✅ Transición detectada: OTRA CLASE")
        return _procesar_otra_clase(session_attributes)
    elif valor == "4":
        print("✅ Transición detectada: OTRA FECHA")
        return _procesar_otra_fecha(session_attributes)
    elif (input_lower in ["no", "no gracias", "5"] or 
          any(p in input_lower for p in ["no", "nada", "gracias", "eso es todo", "ninguna", "no gracias", "nada mas"])):
        print("✅ Transición detectada: NO MÁS CONSULTAS")
        # Procesar directamente aquí usando responder_con_pregunta_final
        return _procesar_no_mas_consultas(session_attributes)
    
    # Si no se detecta transición válida, mostrar error
    print(f"❌ Transición no reconocida: '{input_transcript}'")
    return _mostrar_error_transicion(session_attributes)

def _procesar_otra_sede(session_attributes):
    """Procesa transición a otra sede"""
    print("✅ Transición: OTRA SEDE")
    ciudad_actual = session_attributes.get("ciudad_nombre") or session_attributes.get("ciudad")
    
    if not ciudad_actual:
        return {
            "sessionState": {
                "dialogAction": {"type": "ElicitSlot", "slotToElicit": "ciudad"},
                "intent": {"name": "ConsultaGrupales", "state": "InProgress", "slots": {}},
                "sessionAttributes": session_attributes
            },
            "messages": [{"contentType": "PlainText", "content": "¿En qué ciudad deseas consultar otra sede?"}]
        }
    
    # Limpiar solo sede y clase, mantener ciudad
    keys_to_remove = ["sede_nombre", "sede_id", "categoria_clase_preguntada", "clase_display", "slots_previos"]
    for key in keys_to_remove:
        session_attributes.pop(key, None)
    
    # Configurar nuevo intent para ConsultaGrupales con ciudad
    slots_nuevos = {
        "ciudad": {
            "value": {
                "originalValue": ciudad_actual,
                "resolvedValues": [ciudad_actual],
                "interpretedValue": ciudad_actual
            },
            "shape": "Scalar"
        }
    }
    
    # Limpiar la bandera de transición
    session_attributes.pop("esperando_transicion_grupales", None)
    session_attributes["en_flujo_activo"] = "ConsultaGrupales"
    
    return {
        "sessionState": {
            "dialogAction": {"type": "ElicitSlot", "slotToElicit": "sede"},
            "intent": {
                "name": "ConsultaGrupales",
                "state": "InProgress",
                "slots": slots_nuevos
            },
            "sessionAttributes": session_attributes
        },
        "messages": [{
            "contentType": "PlainText",
            "content": f"¡Perfecto! Te ayudo a consultar otra sede en {ciudad_actual}. ¿En qué sede deseas consultar?"
        }]
    }

def _mostrar_error_transicion(session_attributes):
    """Muestra mensaje de error cuando no se reconoce la transición"""
    
    return {
        "sessionState": {
            "dialogAction": {"type": "ElicitIntent"},
            "intent": {"name": "ConsultaGrupales", "state": "Fulfilled"},
            "sessionAttributes": session_attributes
        },
        "messages": [{
            "contentType": "PlainText",
            "content": (
                "🤔 No entendí tu respuesta. Por favor, selecciona una opción válida:\n\n"
                "1️⃣ Otra ciudad\n"
                "2️⃣ Otra sede\n"
                "3️⃣ Otra clase\n"
                "4️⃣ Otra fecha\n"
                "5️⃣ No gracias\n\n"
                "🏠 M Menú principal\n\n"
                "Responde con el número (1, 2, 3, 4, 5 ó M para volver al menu principal):"
            )
        }]
    }


def _procesar_otra_ciudad(session_attributes):
    """Procesa transición a otra ciudad"""
    print("✅ Transición: OTRA CIUDAD")
    
    # Limpiar toda la información geográfica
    keys_to_remove = [
        "categoria_clase_preguntada", "clase_display", "slots_previos",
        "sede_nombre", "sede_id", "ciudad_nombre", "ciudad_id", "ciudad"
    ]
    for key in keys_to_remove:
        session_attributes.pop(key, None)
    
    # Limpiar la bandera de transición
    session_attributes.pop("esperando_transicion_grupales", None)
    session_attributes["en_flujo_activo"] = "ConsultaGrupales"
    
    return {
        "sessionState": {
            "dialogAction": {"type": "ElicitSlot", "slotToElicit": "ciudad"},
            "intent": {
                "name": "ConsultaGrupales",
                "state": "InProgress",
                "slots": {}
            },
            "sessionAttributes": session_attributes
        },
        "messages": [{
            "contentType": "PlainText",
            "content": "¡Perfecto! 🌎 ¿En qué ciudad deseas consultar las clases grupales?"
        }]
    }


def _procesar_otra_clase(session_attributes):
    """Procesa transición a otra clase"""
    print("✅ Transición: OTRA CLASE")
    ciudad_actual = session_attributes.get("ciudad_nombre") or session_attributes.get("ciudad")
    sede_actual = session_attributes.get("sede_nombre")
    sede_id = session_attributes.get("sede_id")
    
    if not ciudad_actual or not sede_actual:
        return {
            "sessionState": {
                "dialogAction": {"type": "ElicitSlot", "slotToElicit": "ciudad"},
                "intent": {"name": "ConsultaGrupales", "state": "InProgress", "slots": {}},
                "sessionAttributes": session_attributes
            },
            "messages": [{"contentType": "PlainText", "content": "¿En qué ciudad y sede deseas consultar otra clase?"}]
        }
    print(f"✅ Ciudad: {ciudad_actual}, Sede: {sede_actual}, Sede ID: {sede_id}")
    
    # Limpiar solo clase info, mantener ciudad y sede
    keys_to_remove = ["clase_display", "slots_previos"]
    for key in keys_to_remove:
        session_attributes.pop(key, None)
    
    # Limpiar la bandera de transición
    session_attributes.pop("esperando_transicion_grupales", None)
    session_attributes["en_flujo_activo"] = "ConsultaGrupales"
    
    print("✅ Configurando slots para mantener ciudad y sede")
    
    # Configurar slots manteniendo ciudad y sede
    slots_nuevos = {
        "ciudad": {
            "value": {
                "originalValue": ciudad_actual,
                "resolvedValues": [ciudad_actual],
                "interpretedValue": ciudad_actual
            },
            "shape": "Scalar"
        },
        "sede": {
            "value": {
                "originalValue": sede_actual,
                "resolvedValues": [sede_actual],
                "interpretedValue": sede_actual
            },
            "shape": "Scalar"
        }
    }
    
    print("✅ Slots configurados, construyendo respuesta")
    
    # Construir mensaje con clases disponibles si es posible
    try:
        # Intentar obtener clases disponibles
        if sede_id:
            from redshift_utils import consultar_clases_por_sede_id
            clases = consultar_clases_por_sede_id(sede_id)
            
            if clases and len(clases) > 0:
                # Extraer nombres de clases
                clases_nombres = []
                for clase in clases:
                    if isinstance(clase, dict) and 'clase' in clase:
                        clases_nombres.append(clase['clase'])
                    elif isinstance(clase, str):
                        clases_nombres.append(clase)
                
                if clases_nombres:
                    mensaje_clases = (
                        f"¡Perfecto! Te ayudo a consultar otra clase en {sede_actual}, {ciudad_actual}. 🏃‍♂️\n\n"
                        f"📋 **Clases disponibles:**\n\n"
                        + "\n".join(f"• {clase}" for clase in clases_nombres)  # Limitar a 10
                        + "\n\n💬 ¿Cuál clase deseas consultar?"
                    )
                else:
                    mensaje_clases = f"¡Perfecto! Te ayudo a consultar otra clase en {sede_actual}, {ciudad_actual}. 🏃‍♂️\n\n¿Qué clase deseas consultar?"
            else:
                mensaje_clases = f"¡Perfecto! Te ayudo a consultar otra clase en {sede_actual}, {ciudad_actual}. 🏃‍♂️\n\n¿Qué clase deseas consultar?"
        else:
            mensaje_clases = f"¡Perfecto! Te ayudo a consultar otra clase en {sede_actual}, {ciudad_actual}. 🏃‍♂️\n\n¿Qué clase deseas consultar?"
    except Exception as e:
        print(f"⚠️ Error obteniendo clases: {str(e)}")
        mensaje_clases = f"¡Perfecto! Te ayudo a consultar otra clase en {sede_actual}, {ciudad_actual}. 🏃‍♂️\n\n¿Qué clase deseas consultar?"
    
    print(f"✅ Mensaje construido: {mensaje_clases[:100]}...")
    
    respuesta = {
        "sessionState": {
            "dialogAction": {"type": "ElicitSlot", "slotToElicit": "clase"},
            "intent": {
                "name": "ConsultaGrupales",
                "state": "InProgress",
                "slots": slots_nuevos
            },
            "sessionAttributes": session_attributes
        },
        "messages": [{
            "contentType": "PlainText",
            "content": mensaje_clases
        }]
    }
    
    print("✅ Respuesta construida correctamente")
    return respuesta



def _procesar_otra_fecha(session_attributes):
    """Procesa transición a otra fecha manteniendo ciudad, sede y clase"""
    print("✅ Transición: OTRA FECHA")
    
    ciudad_actual = session_attributes.get("ciudad_nombre") or session_attributes.get("ciudad")
    sede_actual = session_attributes.get("sede_nombre")
    clase_actual = session_attributes.get("clase_nombre")
    
    if not ciudad_actual or not sede_actual:
        print("❌ Faltan datos de ciudad o sede para otra fecha")
        return {
            "sessionState": {
                "dialogAction": {"type": "ElicitSlot", "slotToElicit": "ciudad"},
                "intent": {"name": "ConsultaGrupales", "state": "InProgress", "slots": {}},
                "sessionAttributes": session_attributes
            },
            "messages": [{"contentType": "PlainText", "content": "¿En qué ciudad y sede deseas consultar otra fecha?"}]
        }
    
    # Limpiar solo datos específicos de fecha, mantener todo lo demás
    keys_to_remove = ["fecha", "esperando_transicion_grupales"]
    for key in keys_to_remove:
        session_attributes.pop(key, None)
    
    # Configurar slots manteniendo ciudad, sede y clase (si existe)
    slots_nuevos = {
        "ciudad": {
            "value": {
                "originalValue": ciudad_actual,
                "resolvedValues": [ciudad_actual],
                "interpretedValue": ciudad_actual
            },
            "shape": "Scalar"
        },
        "sede": {
            "value": {
                "originalValue": sede_actual,
                "resolvedValues": [sede_actual],
                "interpretedValue": sede_actual
            },
            "shape": "Scalar"
        }
    }
    
    # Si hay clase específica, mantenerla también
    if clase_actual:
        slots_nuevos["clase"] = {
            "value": {
                "originalValue": clase_actual,
                "resolvedValues": [clase_actual],
                "interpretedValue": clase_actual
            },
            "shape": "Scalar"
        }
        mensaje_fecha = f"¡Perfecto! Te ayudo a consultar {clase_actual} en la sede {sede_actual} ({ciudad_actual}) para otra fecha. 📅"
    else:
        mensaje_fecha = f"¡Perfecto! Te ayudo a consultar las clases en la sede {sede_actual} ({ciudad_actual}) para otra fecha. 📅"
    
    # Marcar que estamos en flujo activo
    session_attributes["en_flujo_activo"] = "ConsultaGrupales"
    
    print(f"✅ Parámetros mantenidos - Ciudad: {ciudad_actual}, Sede: {sede_actual}, Clase: {clase_actual or 'Todas'}")
    
    return {
        "sessionState": {
            "dialogAction": {"type": "ElicitSlot", "slotToElicit": "fecha"},
            "intent": {
                "name": "ConsultaGrupales",
                "slots": slots_nuevos,
                "state": "InProgress"
            },
            "sessionAttributes": session_attributes
        },
        "messages": [{
            "contentType": "PlainText",
            "content": f"{mensaje_fecha}\n\n¿Para qué fecha deseas consultar? Puedes escribir:\n• YYYY-MM-DD (2025-01-15)\n• DD de MMMM (15 de enero)\n• DD/MM (15/01)\n• 'hoy' o 'mañana'"
        }]
    }


def _procesar_no_mas_consultas(session_attributes):
    """Procesa cuando el usuario no quiere más consultas - AQUÍ VA LA PREGUNTA FINAL"""
    print("✅ Usuario no desea más consultas - enviando pregunta final")
    
    # Limpiar todo de ConsultaGrupales
    keys_to_remove = [
        "en_flujo_activo", "clase_display", "slots_previos",
        "ciudad_nombre", "sede_nombre", "ciudad_id", "sede_id",
        "esperando_transicion_grupales"
    ]
    for key in keys_to_remove:
        session_attributes.pop(key, None)
    
    # USAR responder_con_pregunta_final en lugar de crear respuesta manual
    return responder_con_pregunta_final(
        "¡Perfecto! 😊", 
        session_attributes, 
        "ConsultaGrupales"
    )

###############################
# Actualizar menús de transición ConsultaGrupales
###############################

def actualizar_menu_transicion_grupales():
    """
    Función para actualizar todos los menús de transición de ConsultaGrupales
    agregando la opción '5️⃣ No gracias' donde falte
    """
    import fileinput
    import sys
    
    archivo = '/Users/gerson.reina/Documents/LexAgenteVirtual_SAC/lambda_function.py'
    
    # Patrón a buscar y reemplazar
    patron_viejo = '"4️⃣ Otra fecha\\n"\n                                "🏠 M Menú principal\\n\\n"'
    patron_nuevo = '"4️⃣ Otra fecha\\n"\n                                "5️⃣ No gracias\\n\\n"\n                                "🏠 M Menú principal\\n\\n"'
    
    # Leer archivo y hacer reemplazos
    with open(archivo, 'r', encoding='utf-8') as f:
        contenido = f.read()
    
    # Hacer el reemplazo
    contenido_actualizado = contenido.replace(
        '"4️⃣ Otra fecha\\n"\n                                "🏠 M Menú principal\\n\\n"',
        '"4️⃣ Otra fecha\\n"\n                                "5️⃣ No gracias\\n\\n"\n                                "🏠 M Menú principal\\n\\n"'
    )
    
    # También actualizar el patrón con más espacios (para las variaciones)
    contenido_actualizado = contenido_actualizado.replace(
        '"4️⃣ Otra fecha\\n"\n                                        "🏠 M Menú principal\\n\\n"',
        '"4️⃣ Otra fecha\\n"\n                                        "5️⃣ No gracias\\n\\n"\n                                        "🏠 M Menú principal\\n\\n"'
    )
    
    # Escribir archivo actualizado
    with open(archivo, 'w', encoding='utf-8') as f:
        f.write(contenido_actualizado)
    
    print("✅ Menús de transición ConsultaGrupales actualizados correctamente")

###############################
# Manejar Transicion ConsultarSedes
###############################

def esperando_respuesta_sedes(session_attributes, input_transcript, slots, intent):
    """
    Maneja toda la lógica de transiciones para ConsultarSedes
    """
    # Solo procesar si realmente está esperando transición Y hay flujo activo
    if (session_attributes.get("esperando_transicion_sedes") != "true" or 
        not session_attributes.get("en_flujo_activo")):
        return None
    
    input_lower = input_transcript.lower().strip()
    print(f"🔍 Analizando transición sedes: '{input_lower}'")
    
    if input_lower in ["m", "menu", "menú", "menu principal", "menú principal"]:
        print("🔄 Usuario pidió ir al menú principal")
        # Limpiar sesión y redirigir a menú principal (puedes reutilizar tu lógica de menú principal aquí)
        acepto_politicas = session_attributes.get("acepto_politicas")
        documento_attrs = {
            "document_type_id": session_attributes.get("document_type_id"),
            "document_type_raw": session_attributes.get("document_type_raw"),
            "document_number": session_attributes.get("document_number"),
            "intenciones_con_documento": session_attributes.get("intenciones_con_documento")
        }
        session_attributes.clear()
        for k, v in documento_attrs.items():
            if v is not None:
                session_attributes[k] = v
        if acepto_politicas == "true":
            session_attributes["acepto_politicas"] = "true"
        return {
            "sessionState": {
                "dialogAction": {"type": "ElicitIntent"},
                "sessionAttributes": session_attributes
            },
            "messages": [{
                "contentType": "PlainText",
                "content": (
                    "Has regresado al menú principal. ¿En qué puedo ayudarte?\n\n"
                    "Algunas opciones:\n"
                    "📄 Preguntas frecuentes sobre Bodytech\n"
                    "🏢 Consultar sedes y horarios\n"
                    "🏃‍♂️ Clases grupales disponibles\n"
                    "📅 Información de tu plan\n"
                    "👥 Consultar invitados\n"
                    "🏆 Información sobre referidos\n"
                    "🧾 Consultar incapacidades\n"
                    "🛍️ Información de ventas\n"
                    "❄️ Consultar congelaciones\n"
                    "¿Sobre qué tema te gustaría que te ayude?"
                )
            }]
        }
    # Detectar transiciones válidas
    if "otra ciudad" in input_lower or input_lower == "1":
        print("✅ Transición detectada: OTRA CIUDAD")
        return _procesar_otra_ciudad_sedes(session_attributes)
    elif "otra sede" in input_lower or input_lower == "2":
        print("✅ Transición detectada: OTRA SEDE") 
        return _procesar_otra_sede_sedes(session_attributes, slots, intent)  # ✅ CAMBIO AQUÍ
    elif (input_lower in ["no", "no gracias", "3"] or 
          any(p in input_lower for p in ["no", "nada", "gracias", "eso es todo", "ninguna", "no gracias", "nada mas"])):
        print("✅ Transición detectada: NO MÁS CONSULTAS")
        return _procesar_no_mas_consultas_sedes(session_attributes)
    
    # Si no se detecta transición válida, mostrar error
    print(f"❌ Transición no reconocida: '{input_transcript}'")
    return _mostrar_error_transicion_sedes(session_attributes)

def _procesar_otra_ciudad_sedes(session_attributes):
    """Procesa transición a otra ciudad para ConsultarSedes"""
    print("✅ Transición: OTRA CIUDAD")
    
    # Limpiar toda la información geográfica
    keys_to_remove = [
        "ciudad", "ciudad_id", "ciudad_nombre", 
        "pregunta_categoria", "consultando_horarios",
        # 🆕 AGREGAR ESTAS LÍNEAS PARA LIMPIAR SEDE ANTERIOR
        "sede_id",
        "sede_nombre",
        "tipo_consulta_temporal",
        "input_original_menu",
        "procesar_con_datos_extraidos"
    ]
    for key in keys_to_remove:
        session_attributes.pop(key, None)
    
    # Limpiar la bandera de transición
    session_attributes.pop("esperando_transicion_sedes", None)
    session_attributes["en_flujo_activo"] = "ConsultarSedes"
    
    return {
        "sessionState": {
            "dialogAction": {"type": "ElicitSlot", "slotToElicit": "ciudad"},
            "intent": {
                "name": "ConsultarSedes",
                "state": "InProgress",
                "slots": {}
            },
            "sessionAttributes": session_attributes
        },
        "messages": [{
            "contentType": "PlainText",
            "content": "¡Perfecto! 🌎 ¿En qué ciudad deseas consultar las sedes?"
        }]
    }

def _procesar_otra_sede_sedes(session_attributes, slots, intent):
    """Procesa la transición 'otra sede' para ConsultarSedes"""
    print("✅ Transición: OTRA SEDE")
    
    # Obtener ciudad actual
    ciudad_actual = session_attributes.get("ciudad_nombre") or session_attributes.get("ciudad")
    ciudad_id = session_attributes.get("ciudad_id")
    
    if not ciudad_actual:
        print("❌ No hay ciudad actual para otra sede")
        return {
            "sessionState": {
                "dialogAction": {"type": "ElicitSlot", "slotToElicit": "ciudad"},
                "intent": {"name": "ConsultarSedes", "state": "InProgress", "slots": {}},
                "sessionAttributes": session_attributes
            },
            "messages": [{
                "contentType": "PlainText",
                "content": "¿En qué ciudad deseas consultar las sedes?"
            }]
        }
    
    print(f"✅ Manteniendo ciudad: {ciudad_actual}")
    print(f"✅ Ciudad ID: {ciudad_id}")
    
    keys_to_remove = [
        "esperando_transicion_sedes", 
        "consultando_horarios",
        "sede_id",  
        "sede_nombre",  
        "tipo_consulta_temporal",  
        "input_original_menu",  
        "procesar_con_datos_extraidos"  
    ]
    for key in keys_to_remove:
        session_attributes.pop(key, None)
    
    session_attributes["pregunta_categoria"] = None
    session_attributes["en_flujo_activo"] = "ConsultarSedes"
    
    print("✅ Configurando para continuar flujo normal...")
    
    return {
        "sessionState": {
            "dialogAction": {"type": "ElicitSlot", "slotToElicit": "ciudad"},
            "intent": {
                "name": "ConsultarSedes",
                "state": "InProgress",
                "slots": {
                    "ciudad": {
                        "value": {
                            "originalValue": ciudad_actual,
                            "resolvedValues": [ciudad_actual],
                            "interpretedValue": ciudad_actual
                        },
                        "shape": "Scalar"
                    }
                }
            },
            "sessionAttributes": session_attributes
        },
        "messages": [{
            "contentType": "PlainText",
            "content": f"¡Perfecto! Te ayudo a consultar otra sede en {ciudad_actual}. ¿En qué ciudad deseas consultar ahora? 🏢"
        }]
    }

def _procesar_no_mas_consultas_sedes(session_attributes):
    """Procesa cuando el usuario no quiere más consultas de sedes"""
    print("✅ Usuario no quiere más consultas")
    
    # Limpiar todo y enviar pregunta final
    keys_to_remove = [
        "en_flujo_activo", "ciudad", "ciudad_id", "ciudad_nombre", 
        "pregunta_categoria", "consultando_horarios", "esperando_transicion_sedes"
    ]
    for key in keys_to_remove:
        session_attributes.pop(key, None)
    
    return responder_con_pregunta_final("¡Perfecto! 😊", session_attributes, "ConsultarSedes")

def _mostrar_error_transicion_sedes(session_attributes):
    """Muestra mensaje de error cuando no se reconoce la transición de sedes"""
    contenido = (
        "🤔 No entendí tu respuesta. Por favor, selecciona una opción válida:\n\n"
        "1️⃣ Otra ciudad\n"
        "2️⃣ Otra sede\n"
        "3️⃣ No gracias\n\n"
        "🏠 M Menú principal\n\n"
        "Responde con el número (1, 2, 3 ó M para volver al menu principal):"
    )
    
    return {
        "sessionState": {
            "dialogAction": {"type": "ElicitIntent"},
            "intent": {"name": "ConsultarSedes", "state": "Fulfilled"},
            "sessionAttributes": session_attributes
        },
        "messages": [{"contentType": "PlainText", "content": contenido}]
    }

###############################
# Responder Calificación
###############################

def obtener_intent_detectado(event):

    try:
        return event.get("sessionState", {}).get("intent", {}).get("name", "")
    except Exception as e:
        print(f"❌ Error obteniendo intent detectado: {str(e)}")
        return ""

def responder_con_pregunta_final(mensaje, session_attributes, intent_name):
    print("🔍 ===== DEBUG responder_con_pregunta_final =====")
    print(f"🔍 intent_name: {intent_name}")
    print(f"🔍 session_attributes recibidos: {session_attributes}")
    
    
    # Si el usuario menciona una nueva intención
    if intent_name in ["ConsultarSedes"]:
        # Limpiar sessionAttributes relacionados con la intención anterior
        session_attributes = {"acepto_politicas": session_attributes.get("acepto_politicas", "true")}
        # Iniciar el intent desde cero
        return {
            "sessionState": {
                "dialogAction": {"type": "ElicitSlot", "slotToElicit": "ciudad"},
                "intent": {
                    "name": intent_name,
                    "slots": {},  # Vacío, para empezar desde cero
                    "state": "InProgress"
                },
                "sessionAttributes": session_attributes
            },
            "messages": [{
                "contentType": "PlainText",
                "content": "¿En qué ciudad deseas consultar los horarios de sede?"
            }]
        }
        
    if intent_name == "ConsultaGrupales":
        print("🔍 Intent es ConsultaGrupales - si marcando esperando_respuesta_final")
        # Limpiar datos específicos de ConsultaGrupales pero mantener ciudad/sede si existen
        keys_to_remove = ["clase_display", "slots_previos"]
        for key in keys_to_remove:
            session_attributes.pop(key, None)
        
        # NO marcar esperando_respuesta_final para ConsultaGrupales
        session_attributes["esperando_respuesta_final"] = "true"
        session_attributes.pop("en_flujo_activo", None)
        
        contenido = f"{mensaje}\n\n¿Necesitas ayuda con algo más? 😊\n\n💬 Responde 'Sí' para ver el menú principal o 'No' para finalizar."
        
        return {
            "sessionState": {
                "dialogAction": {"type": "ElicitIntent"},
                "intent": {
                    "name": intent_name, 
                    "state": "Fulfilled"
                },
                "sessionAttributes": session_attributes
            },
            "messages": [{
                "contentType": "PlainText",
                "content": contenido
            }]
        }
    
    # Para todas las demás intenciones, comportamiento normal
    session_attributes.pop("en_flujo_activo", None)
    
    session_attributes.pop("clase_display", None)
    session_attributes.pop("slots_previos", None)
    session_attributes["esperando_respuesta_final"] = "true"
    
    contenido = f"{mensaje}\n\n¿Necesitas ayuda con algo más? 😊\n\n💬 Responde 'Sí' para ver el menú principal o 'No' para finalizar."
    
    return {
        "sessionState": {
            "dialogAction": {"type": "ElicitIntent"},
            "intent": {
                "name": intent_name, 
                "state": "Fulfilled"
            },
            "sessionAttributes": session_attributes
        },
        "messages": [{
            "contentType": "PlainText",
            "content": contenido
        }]
    }
# Función para manejo de información adicional
def crear_respuesta_info_adicional(mensaje_final, session_attributes, intent_name, flag_key):
    session_attributes[flag_key] = "true"
    

    contenido = f"{mensaje_final}\n\n¿Deseas más información sobre este tema?\n\nResponde: 'Sí' o 'No'"

    return {
        "sessionState": {
            "dialogAction": {"type": "ElicitIntent"},
            "sessionAttributes": session_attributes
        },
        "messages": [{
            "contentType": "PlainText",
            "content": contenido
        }]
    }

def manejar_consulta_horarios(intent, session_attributes, slots, input_transcript):
    print("Entrando a manejar_consulta_horarios")
    tipo_horario = slots.get("tipo_horario")
    if not tipo_horario:
        # Preguntar primero el tipo de horario
        return {
        "sessionState": {
            "dialogAction": {"type": "ElicitSlot", "slotToElicit": "tipo_horario"},
            "intent": {
                "name": "ConsultaHorarios",
                "slots": slots,
                "state": "InProgress"
            },
            "sessionAttributes": session_attributes
        },
        "messages": [{
            "contentType": "PlainText",
            "content": (
                "¿Qué tipo de horarios deseas consultar? 📅\n\n"
                "🏃‍♂️ **Clases grupales** - Horarios de actividades específicas\n"
                "🏢 **Sede** - Horarios de atención general\n\n"
                "💬 **Responde:** 'Clases grupales' o 'Sede'"
            )
        }]
    }
    # Aquí defines tu condición real
    if input_transcript.strip().lower() in ["no", "no gracias"]:
        print("Retornando responder_con_pregunta_final desde manejar_consulta_horarios")
        return responder_con_pregunta_final(
            "¡Perfecto! 😊",
            session_attributes,
            "ConsultaHorarios"
        )

    print("Saliendo de manejar_consulta_horarios sin return")
    return None

###############################
# Verificar si es input valido
###############################

def es_input_valido(input_transcript):
    """
    Valida si el input del usuario es texto coherente o solo caracteres sin sentido
    """
    import re
    
    # Limpiar el input
    texto_limpio = input_transcript.strip().lower()
    
    # 1. Si está vacío, es inválido
    if not texto_limpio:
        return False
    
    # ✅ NUEVO: Si es solo números (posible documento), es válido
    if texto_limpio.isdigit() and 4 <= len(texto_limpio) <= 15:
        print(f"✅ Input válido (número de documento): '{texto_limpio}'")
        return True
    
    # 2. Si es solo números largos sin sentido (más de 15 dígitos)
    if texto_limpio.isdigit() and len(texto_limpio) > 15:
        return False
    
    # 3. Si tiene más del 70% de caracteres repetidos o sin sentido
    caracteres_unicos = len(set(texto_limpio.replace(" ", "")))
    total_caracteres = len(texto_limpio.replace(" ", ""))
    
    if total_caracteres > 5 and caracteres_unicos / total_caracteres < 0.3:
        print(f"🚫 Texto con pocos caracteres únicos: {caracteres_unicos}/{total_caracteres}")
        return False
    
    # 4. ✅ MEJORAR: Detectar patrones de tecleo aleatorio (pero excluir palabras válidas)
    # Lista de palabras válidas que pueden tener muchas consonantes
    palabras_validas_consonantes = [
        "country", "centro", "rumba", "spinning", "crossfit", "strength", "strong",
        "chapinero", "normandia", "outlet", "portal", "tintal", "hayuelos", "cedritos",
        "horarios", "tienen", "grupales", "clases", "horario", "consultar"
    ]
    
    # Solo aplicar filtro de consonantes si NO contiene palabras válidas conocidas
    contiene_palabra_valida = any(palabra in texto_limpio for palabra in palabras_validas_consonantes)
    
    if not contiene_palabra_valida:
        patron_sin_sentido = re.compile(r'[bcdfghjklmnpqrstvwxyz]{6,}')  # Aumentar umbral a 6
        if patron_sin_sentido.search(texto_limpio):
            print(f"🚫 Patrón de consonantes detectado: {texto_limpio}")
            return False
    
    # 5. Detectar secuencias de teclado obvias
    secuencias_teclado = [
        'qwerty', 'asdf', 'zxcv', 'qaz', 'wsx', 'edc', 'rfv', 'tgb', 'yhn', 'ujm',
        'qlllq', 'asklj', 'lkjh', 'mnbv', 'poiu', 'wert', 'dfgh', 'cvbn'
    ]
    
    for secuencia in secuencias_teclado:
        if secuencia in texto_limpio:
            print(f"🚫 Secuencia de teclado detectada: {secuencia}")
            return False
    
    # 6. Si es muy corto pero no tiene sentido (menos de 3 caracteres válidos)
    if len(texto_limpio.replace(" ", "")) < 3 and not any(palabra in texto_limpio for palabra in [
        "si", "no", "ok", "hola", "bye", "m", "n", "1", "2", "3", "4", "5", "6", "7", "8", "9"
    ]):
        return False
    
    # 7. Detectar si NO tiene ninguna vocal (excepto números y palabras muy cortas)
    if (len(texto_limpio) > 2 and 
        not re.search(r'[aeiouáéíóú]', texto_limpio) and 
        not texto_limpio.isdigit()):  # ✅ AGREGAR esta condición
        print(f"🚫 Texto sin vocales: {texto_limpio}")
        return False
    
    print(f"✅ Input válido: '{texto_limpio}'")
    return True

def procesar_input_original_sedes(input_original, session_attributes):
    """
    Procesa el input original para extraer automáticamente datos de ConsultarSedes
    """
    print(f"🎯 Procesando input original para ConsultarSedes: '{input_original}'")
    print(f"🔍 DEBUG session_attributes recibidos: {session_attributes}")
    
    try:
        # Importar la función de extracción de sedes
        from services import extraer_y_validar_slots_sedes
        
        # Crear un intent mock
        intent_mock = {
            "name": "ConsultarSedes",
            "slots": {}
        }
        
        print(f"🔍 Llamando extraer_y_validar_slots_sedes con: input='{input_original}', intent={intent_mock}")
        
        # Procesar el input original
        resultado = extraer_y_validar_slots_sedes(input_original, session_attributes, intent_mock)
        
        print(f"🔍 Resultado de extracción automática: {resultado}")
        print(f"🔍 Tipo de resultado: {type(resultado)}")
        
        if resultado:
            print(f"🔍 Keys en resultado: {list(resultado.keys()) if isinstance(resultado, dict) else 'No es dict'}")
            
        # Si la función devuelve una respuesta directa (consulta exitosa)
        if resultado and resultado.get("sessionState"):
            print("✅ Extracción exitosa - devolviendo respuesta directa")
            return resultado
        
        # Si es consulta directa, EJECUTAR la consulta real
        if resultado and resultado.get("consulta_directa") == True:
            print("✅ Consulta directa detectada - EJECUTANDO CONSULTA REAL")
            
            # Extraer datos necesarios
            sede_id = resultado.get("sede_id")
            sede_nombre = resultado.get("sede_nombre")
            ciudad_nombre = resultado.get("ciudad_nombre")
            tipo_consulta = resultado.get("tipo_consulta")
            
            print(f"🔍 Datos extraídos - sede_id: {sede_id}, sede_nombre: {sede_nombre}, tipo_consulta: {tipo_consulta}")
            
            # EJECUTAR LA CONSULTA REAL según el tipo
            if tipo_consulta == "horarios_sede" and sede_id:
                print(f"🎯 Ejecutando consulta de horarios para sede {sede_nombre} (ID: {sede_id})")
                
                # Importar función de consulta de horarios
                from redshift_utils import consultar_horarios_sede
                from respuestas import respuesta_bedrock
                
                # Ejecutar consulta real
                horarios = consultar_horarios_sede(sede_id)
                
                if not horarios:
                    mensaje_final = f"No se encontraron horarios para la sede {sede_nombre.title()}."
                else:
                    print(f"✅ Horarios encontrados: {len(horarios) if isinstance(horarios, list) else 'datos disponibles'}")
                    # Generar respuesta con Bedrock
                    mensaje_final = respuesta_bedrock("ConsultarSedes", horarios)
                    if not mensaje_final or not mensaje_final.strip():
                        mensaje_final = f"Horarios de atención de la sede {sede_nombre} en {ciudad_nombre}:\n\n📅 Consulta completada exitosamente."
                
                # session_attributes con los datos
                if resultado.get("session_attributes"):
                    for key, value in resultado["session_attributes"].items():
                        if isinstance(value, (str, int, float, bool)):
                            session_attributes[key] = str(value)
                
                # CONFIGURAR PARA PREGUNTA DE TRANSICIÓN
                session_attributes["esperando_transicion_sedes"] = "true"
                session_attributes["en_flujo_activo"] = "ConsultarSedes"
                
                return {
                    "sessionState": {
                        "dialogAction": {"type": "ElicitSlot", "slotToElicit": "tipo_transicion"},
                        "intent": {
                            "name": "ConsultarSedes",
                            "state": "InProgress",
                            "slots": {}
                        },
                        "sessionAttributes": session_attributes
                    },
                    "messages": [{
                        "contentType": "PlainText",
                        "content": (
                            f"{mensaje_final}\n\n"
                            "¿Deseas hacer otra consulta de sedes? 🏢\n\n"
                            "Selecciona una opción:\n"
                            "1️⃣ Otra ciudad\n"
                            "2️⃣ Otra sede\n"
                            "3️⃣ No gracias\n\n"
                            "🏠 M Menú principal\n"
                            "💬 Escribe el número de tu opción o M para volver al menú principal:"
                        )
                    }]
                }
            
            # OTROS TIPOS DE CONSULTA (categoria_especifica, sede_especifica, etc.)
            elif tipo_consulta == "sede_especifica" and sede_id:
                print(f"🎯 Ejecutando consulta específica para sede {sede_nombre}")
                
                from redshift_utils import consultar_horarios_sede
                from respuestas import respuesta_bedrock
                
                horarios = consultar_horarios_sede(sede_id)
                if not horarios:
                    mensaje_final = f"No se encontraron horarios para la sede {sede_nombre.title()}."
                else:
                    mensaje_final = respuesta_bedrock("ConsultarSedes", horarios)
                    if not mensaje_final or not mensaje_final.strip():
                        mensaje_final = f"Información de la sede {sede_nombre} en {ciudad_nombre}."
                
                # Actualizar session_attributes y configurar transición
                if resultado.get("session_attributes"):
                    for key, value in resultado["session_attributes"].items():
                        if isinstance(value, (str, int, float, bool)):
                            session_attributes[key] = str(value)
                
                session_attributes["esperando_transicion_sedes"] = "true"
                session_attributes["en_flujo_activo"] = "ConsultarSedes"
                
                return {
                    "sessionState": {
                        "dialogAction": {"type": "ElicitSlot", "slotToElicit": "tipo_transicion"},
                        "intent": {
                            "name": "ConsultarSedes",
                            "state": "InProgress",
                            "slots": {}
                        },
                        "sessionAttributes": session_attributes
                    },
                    "messages": [{
                        "contentType": "PlainText",
                        "content": (
                            f"{mensaje_final}\n\n"
                            "¿Deseas hacer otra consulta de sedes? 🏢\n\n"
                            "Selecciona una opción:\n"
                            "1️⃣ Otra ciudad\n"
                            "2️⃣ Otra sede\n" 
                            "3️⃣ No gracias\n\n"
                            "🏠 M Menú principal\n"
                            "💬 Escribe el número de tu opción o M para volver al menú principal:"
                        )
                    }]
                }
            
            # Si no se puede ejecutar consulta directa, continuar flujo normal mejorado
            else:
                print("⚠️ Consulta directa detectada pero no se puede ejecutar - mejorando flujo normal")
                
                # Actualizar session_attributes con datos extraídos
                if resultado.get("session_attributes"):
                    for key, value in resultado["session_attributes"].items():
                        if isinstance(value, (str, int, float, bool)):
                            session_attributes[key] = str(value)
                
                session_attributes["input_original_menu"] = str(input_original)
                session_attributes["mejoramiento_sedes"] = "true"
                
                return {
                    "sessionState": {
                        "dialogAction": {"type": "ElicitIntent"},
                        "intent": {
                            "name": "ConsultarSedes",
                            "state": "InProgress",
                            "slots": {}
                        },
                        "sessionAttributes": session_attributes
                    },
                    "messages": [{
                        "contentType": "PlainText",
                        "content": f"¡Perfecto! Te ayudo a consultar información de {sede_nombre if sede_nombre else 'sedes'} 🏢"
                    }]
                }
        
        # Si se detectaron parámetros parciales (ej: solo ciudad), seguir flujo normal con datos
        if resultado and (resultado.get("ciudad_id") or resultado.get("sede_id") or resultado.get("categoria_nombre")):
            print("✅ Parámetros parciales detectados - mejorando flujo normal")
            
            # Actualizar session_attributes con solo strings
            if resultado.get("ciudad_id"):
                session_attributes["ciudad_id"] = str(resultado["ciudad_id"])
            if resultado.get("ciudad_nombre"):
                session_attributes["ciudad_nombre"] = str(resultado["ciudad_nombre"])
            if resultado.get("sede_id"):
                session_attributes["sede_id"] = str(resultado["sede_id"])
            if resultado.get("sede_nombre"):
                session_attributes["sede_nombre"] = str(resultado["sede_nombre"])
            
            session_attributes["input_original_menu"] = str(input_original)
            session_attributes["mejoramiento_sedes"] = "true"
            
            return {
                "sessionState": {
                    "dialogAction": {"type": "ElicitIntent"},
                    "intent": {
                        "name": "ConsultarSedes",
                        "state": "InProgress",
                        "slots": {}
                    },
                    "sessionAttributes": session_attributes
                },
                "messages": [{
                    "contentType": "PlainText",
                    "content": "¡Perfecto! Te ayudo a consultar información de sedes y horarios 🏢"
                }]
            }
        
        # Si no se pudo extraer automáticamente, seguir flujo normal
        print("⚠️ No se pudieron extraer datos automáticamente - siguiendo flujo normal")
        return None
        
    except Exception as e:
        print(f"❌ Error procesando input original para sedes: {str(e)}")
        return None

def manejar_timeout_sesion(session_attributes, input_transcript=""):
    """
    Maneja el timeout de sesión con flujo de 3 min + 2 min
    - 3 min sin respuesta: pregunta si puede ayudar en algo más
    - 2 min adicionales sin respuesta: finaliza la sesión
    """
    import time
    
    # Obtener timestamp actual
    timestamp_actual = int(time.time())
    
    ultimo_intercambio = session_attributes.get("ultimo_intercambio")
    primer_aviso_timestamp = session_attributes.get("primer_aviso_timeout")
    esperando_respuesta_timeout = session_attributes.get("esperando_respuesta_timeout")
    
    # Si no hay timestamp, es la primera interacción
    if not ultimo_intercambio:
        session_attributes["ultimo_intercambio"] = str(timestamp_actual)
        return None
    
    # Calcular tiempo transcurrido desde la última interacción
    tiempo_inactivo = timestamp_actual - int(ultimo_intercambio)
    
    print(f"🕐 Tiempo inactivo: {tiempo_inactivo} segundos")
    
    # ===== CASO 1: PRIMERA INACTIVIDAD (3 minutos) =====
    if tiempo_inactivo > 180 and not primer_aviso_timestamp:  # 3 minutos = 180 segundos
        print("⏰ PRIMER TIMEOUT (3 min) - Preguntando si necesita ayuda")
        session_attributes["primer_aviso_timeout"] = str(timestamp_actual)
        session_attributes["esperando_respuesta_timeout"] = "true"
        
        return {
            "sessionState": {
                "dialogAction": {"type": "ElicitIntent"},
                "sessionAttributes": session_attributes
            },
            "messages": [{
                "contentType": "PlainText",
                "content": (
                    "🤔 He notado que ha pasado un tiempo sin actividad.\n\n"
                    "¿Puedo ayudarte en algo más?\n\n"
                    "💬 Responde 'sí' si necesitas ayuda o cualquier consulta que tengas."
                )
            }]
        }
    
    # ===== CASO 2: YA SE HIZO LA PRIMERA PREGUNTA =====
    elif primer_aviso_timestamp and esperando_respuesta_timeout == "true":
        tiempo_desde_primer_aviso = timestamp_actual - int(primer_aviso_timestamp)
        
        # Si hay input del usuario, procesar su respuesta
        if input_transcript.strip():
            print(f"✅ Usuario respondió después del primer aviso: '{input_transcript}'")
            
            # Limpiar flags de timeout
            session_attributes.pop("esperando_respuesta_timeout", None)
            session_attributes.pop("primer_aviso_timeout", None)
            
            # Actualizar timestamp de última actividad
            session_attributes["ultimo_intercambio"] = str(timestamp_actual)
            
            # Procesar la respuesta
            respuesta_normalizada = input_transcript.lower().strip()
            
            # Respuestas afirmativas - continuar sesión
            if any(palabra in respuesta_normalizada for palabra in [
                "si", "sí", "ayuda", "necesito", "quiero", "claro", "vale", "ok", "yes"
            ]):
                return mostrar_menu_principal(session_attributes)
            
            # Respuestas negativas - finalizar sesión amigablemente
            elif any(palabra in respuesta_normalizada for palabra in [
                "no", "nada", "gracias", "finalizar", "terminar", "adiós", "bye", "chao"
            ]):
                return finalizar_sesion_timeout_negativa()
            
            # Respuesta no clara - dar una oportunidad más
            else:
                return {
                    "sessionState": {
                        "dialogAction": {"type": "ElicitIntent"},
                        "sessionAttributes": session_attributes
                    },
                    "messages": [{
                        "contentType": "PlainText",
                        "content": (
                            "🤔 No entendí tu respuesta.\n\n"
                            "¿Necesitas ayuda con algo más?\n\n"
                            "💬 Responde 'sí' para continuar o 'no' para finalizar."
                        )
                    }]
                }
        
        # ===== CASO 3: SIN RESPUESTA DESPUÉS DE 2 MINUTOS ADICIONALES =====
        elif tiempo_desde_primer_aviso > 120:  # 2 minutos = 120 segundos
            print("⏰ TIMEOUT FINAL (2 min adicionales) - Finalizando sesión automáticamente")
            return finalizar_sesion_timeout_automatico()
    
    # ===== ACTUALIZAR TIMESTAMP EN INTERACCIONES NORMALES =====
    if input_transcript.strip():
        session_attributes["ultimo_intercambio"] = str(timestamp_actual)
    
    return None

def finalizar_sesion_timeout_negativa():
    """
    Finaliza la sesión cuando el usuario dice que no necesita más ayuda
    """
    return {
        "sessionState": {
            "dialogAction": {"type": "Close"},
            "intent": {
                "name": "TimeoutFinalizado",
                "state": "Fulfilled"
            },
            "sessionAttributes": {"conversacion_finalizada": "true"}
        },
        "messages": [{
            "contentType": "PlainText",
            "content": (
                "¡Gracias por contactarte con Bodytech! 😊\n\n"
                "Ha sido un placer ayudarte. Que tengas un excelente día.\n\n"
                "Puedes iniciar una nueva conversación cuando lo desees."
            )
        }]
    }

def finalizar_sesion_timeout_automatico():
    """
    Finaliza la sesión automáticamente por falta de respuesta
    """
    return {
        "sessionState": {
            "dialogAction": {"type": "Close"},
            "intent": {
                "name": "TimeoutFinalizado", 
                "state": "Fulfilled"
            },
            "sessionAttributes": {"conversacion_finalizada": "true"}
        },
        "messages": [{
            "contentType": "PlainText",
            "content": (
                "⏰ **Sesión finalizada por inactividad**\n\n"
                "No hemos recibido respuesta en los últimos minutos, "
                "por lo que hemos cerrado la conversación.\n\n"
                "¡Gracias por contactarte con Bodytech! 😊\n"
                "Puedes iniciar una nueva conversación cuando lo desees."
            )
        }]
    }

###############################
# Menú Principal Interactivo
###############################

def mostrar_menu_principal(session_attributes):
    """
    Muestra el menú principal con opciones numeradas para navegar a cada intención
    """
    menu_contenido = (
        "Perfecto! ¿En qué puedo ayudarte?\n\n"
        "1️⃣ 📋 **Preguntas frecuentes sobre BodyTech**\n"
        "2️⃣ 🏢 **Consultar sedes y horarios**\n" 
        "3️⃣ 🏃‍♂️ **Clases grupales disponibles**\n"
        "4️⃣ 💪 **Información de tu plan**\n"
        "5️⃣ 👥 **Consultar invitados**\n"
        "6️⃣ 🏆 **Información sobre referidos**\n"
        "7️⃣ 🧾 **Consultar incapacidades**\n"
        "8️⃣ 💼 **Información de ventas**\n"
        "9️⃣ ❄️ **Sobre tema de congelaciones**\n\n"
        "🚪 **N** - No quiero más ayuda (Finalizar la Conversación)\n\n"
        "💬 También puedes escribir directamente en qué necesitas ayuda\n"
        "🔙 Recuerda que siempre puedes escribir la **M** para volver al menú principal\n\n"
        "Responde con el **número** de tu opción (1-9), **N** para finalizar ó escribeme tu consulta:"
    )
    
    session_attributes["esperando_seleccion_menu"] = "true"
    
    return {
        "sessionState": {
            "dialogAction": {"type": "ElicitIntent"},
            "sessionAttributes": session_attributes
        },
        "messages": [{
            "contentType": "PlainText",
            "content": menu_contenido
        }]
    }

def procesar_seleccion_menu(input_transcript, session_attributes):
    """
    Procesa la selección del usuario del menú principal
    Permite tanto números (1-9, N) como texto libre
    """
    print(f"🔍 Procesando selección: '{input_transcript}'")
    
    input_limpio = input_transcript.strip().lower()
    
    # Mapeo de opciones a intenciones
    opciones_menu = {
        "1": "FQABodytech",
        "2": "ConsultarSedes", 
        "3": "ConsultaGrupales",
        "4": "ConsultaInfoPlan",
        "5": "ConsultarInvitados",
        "6": "FQAReferidos",
        "7": "ConsultaIncapacidades",
        "8": "Venta",
        "9": "CongelarPlan"
    }
    
    # Limpiar la bandera del menú
    session_attributes.pop("esperando_seleccion_menu", None)
    
    # PRIORIDAD 1: Opción N - Finalizar conversación
    if input_limpio in ["n", "no", "no quiero mas ayuda", "finalizar", "terminar"]:
        print("🚪 Usuario eligió finalizar desde menú principal")
        return cerrar_conversacion("¡Perfecto! Vamos a finalizar.", session_attributes)
    
    # PRIORIDAD 2: Opciones 1-9 - Redirigir a intenciones
    elif input_limpio in opciones_menu:
        intent_seleccionado = opciones_menu[input_limpio]
        print(f"✅ Usuario seleccionó opción {input_limpio}: {intent_seleccionado}")
        
        resultado_redireccion = redirigir_a_intencion(intent_seleccionado, session_attributes)
        
        # ✅ MANEJAR EL CASO CUANDO RETORNA None
        if resultado_redireccion is None:
            print("🔄 Redireccion retornó None - necesita procesar en lambda_handler")
            # Retornar datos para que lambda_handler continúe el flujo
            return {
                "continuar_flujo": True,
                "intent_name": intent_seleccionado,
                "session_attributes": session_attributes
            }
        else:
            # Si la redirección retorna una respuesta, enviarla
            return resultado_redireccion
    
    # VALIDACIÓN PREVIA: Verificar si el input tiene sentido antes de enviar a Bedrock
    elif not es_input_valido(input_transcript):
        print(f"🚫 Input inválido detectado: '{input_transcript}' - No enviando a Bedrock")
        
        session_attributes["esperando_seleccion_menu"] = "true"
        
        return {
            "sessionState": {
                "dialogAction": {"type": "ElicitIntent"},
                "sessionAttributes": session_attributes
            },
            "messages": [{
                "contentType": "PlainText",
                "content": (
                    f"🤔 No entendí tu entrada: '{input_transcript}'\n\n"
                    "Por favor, usa:\n"
                    "• **Números del 1 al 9** para opciones específicas\n"
                    "• ** ó Texto claro** como 'horarios de yoga', 'info de mi plan', 'consulta de plan' etc..\n"
                    "• **N** para finalizar\n\n"
                    "💬 ¿Qué te gustaría hacer?"
                )
            }]
        }
    
    # PRIORIDAD 3: Permitir texto libre y clasificar con Bedrock
    else:
        print(f"🔍 Texto libre detectado: '{input_transcript}' - Analizando clasificación")
        
        # 🆕 FUNCIONES DE NORMALIZACIÓN LOCAL
        def normalizar_texto_busqueda(texto):
            """Normaliza texto para búsqueda: quita tildes, convierte a minúsculas, etc."""
            import unicodedata
            texto = texto.lower().strip()
            # Quitar tildes y caracteres especiales
            texto_sin_tildes = ''.join(c for c in unicodedata.normalize('NFD', texto) 
                                     if unicodedata.category(c) != 'Mn')
            return texto_sin_tildes
        
        def corregir_clases_comunes(texto):
            """Corrige errores comunes en nombres de clases"""
            correcciones_clases = {
                # Errores de tipeo comunes
                "yga": "yoga", "yogga": "yoga", "ioga": "yoga",
                "piltes": "pilates", "pilats": "pilates", "pilaties": "pilates",
                "samba": "zumba", "sumba": "zumba", "kumba": "zumba",
                "spining": "spinning", "spinnig": "spinning", "espin": "spinning",
                "rumbas": "rumba", "rumva": "rumba", "runba": "rumba",
                "acqua": "aqua", "agua": "aqua", "akua": "aqua",
                "funcinal": "funcional", "funcinal": "funcional",
                "crosfit": "crossfit", "crossfitt": "crossfit","cycling": "cyclingtech", "cyclintech"
                # Variaciones locales
                "aerobicos": "aerobicos", "aerovicos": "aerobicos",
                "abdominales": "abdomen", "abs": "abdomen",
                "gluteos": "glúteos", "gluteoss": "glúteos",
                "natacion": "natacion", "nataciom": "natacion"
            }
            
            texto_normalizado = normalizar_texto_busqueda(texto)
            for error, correccion in correcciones_clases.items():
                if error in texto_normalizado:
                    texto_normalizado = texto_normalizado.replace(error, correccion)
            return texto_normalizado
        
        def corregir_sedes_comunes(texto):
            """Corrige errores comunes en nombres de sedes"""
            correcciones_sedes = {
                # Errores de tipeo comunes
                "normandie": "normandia", "normadie": "normandia", "normadia": "normandia",
                "chiko": "chico", "chic": "chico",
                "centro maior": "centro mayor", "centromayor": "centro mayor",
                "chapinro": "chapinero", "chaponero": "chapinero",
                "zona roza": "zona rosa", "zonarosa": "zona rosa",
                "unicentro": "unicentro", "uni centro": "unicentro",
                "santafe": "santafe", "santa fe": "santafe",
                "carrera15": "carrera 15", "carrera quinze": "carrera 15",
                "cedritos": "cedritos", "sedritos": "cedritos",
                "hayuelos": "hayuelos", "alleuelos": "hayuelos",
                "buenavista": "buenavista", "buena vista": "buenavista",
                "outlets": "outlet", "outllet": "outlet",
                # AGREGAR CORRECCIONES PARA COUNTRY
                "contry": "country", "countri": "country", "cuntry": "country",
                "club country": "country", "country club": "country",
                # Ciudades comunes mal escritas
                "bogot": "bogota", "bogote": "bogota", "bogto": "bogota",
                "medelin": "medellin", "medelein": "medellin", "medallo": "medellin",
                "bucamaranga": "bucaramanga", "bucaranga": "bucaramanga",
                "cucta": "cucuta", "cuccuta": "cucuta",
                "baranquilla": "barranquilla", "varranquilla": "barranquilla"
            }
            
            texto_normalizado = normalizar_texto_busqueda(texto)
            for error, correccion in correcciones_sedes.items():
                if error in texto_normalizado:
                    texto_normalizado = texto_normalizado.replace(error, correccion)
            return texto_normalizado
        
        # 🆕 DETECCIÓN LOCAL MEJORADA CON NORMALIZACIÓN
        clases_conocidas = [
            "yoga", "pilates", "zumba", "spinning", "rumba", "danza", "aqua", "funcional", 
            "crossfit", "bodypump", "bodycombat", "bodybalance", "bodyattack", "gap",
            "abdomen", "glúteos", "stretching", "power", "strong", "bootcamp", "natacion",
            "aquaerobicos", "boxeo", "kickboxing", "tabata", "hiit", "cardio", "aerobicos",
            "step", "bailoterapia", "ritmos", "salsa", "bachata", "merengue", "twerk", "cycling", "cyclintech"
        ]
        
        # Palabras clave que indican consulta de clases grupales
        palabras_clave_grupales = ["horario", "horarios", "clase", "clases", "actividad", "actividades"]
        
        # 🆕 NORMALIZAR EL INPUT ANTES DE BUSCAR
        input_normalizado = corregir_clases_comunes(input_transcript)
        input_lower = input_normalizado.lower()
        
        print(f"🔍 Input original: '{input_transcript}'")
        print(f"🔍 Input normalizado: '{input_normalizado}'")
        
        # Si menciona una clase conocida + palabra clave, es definitivamente ConsultaGrupales
        for clase in clases_conocidas:
            if (clase in input_lower and 
                any(palabra in input_lower for palabra in palabras_clave_grupales)):
                print(f"✅ Detección local: '{clase}' + palabra clave encontrada en '{input_transcript}' → ConsultaGrupales")
                session_attributes["input_original_menu"] = input_transcript
                return redirigir_a_intencion("ConsultaGrupales", session_attributes)
        
        # También detectar patrones específicos sin necesidad de palabra clave adicional
        patrones_directos = [
            "horarios de", "horario de", "clases de", "clase de", 
            "que horarios", "qué horarios", "cuando es", "cuándo es",
            "a que hora", "a qué hora"
        ]
        
        for patron in patrones_directos:
            if patron in input_lower:
                for clase in clases_conocidas:
                    if clase in input_lower:
                        print(f"✅ Detección local directa: patrón '{patron}' + clase '{clase}' → ConsultaGrupales")
                        session_attributes["input_original_menu"] = input_transcript
                        return redirigir_a_intencion("ConsultaGrupales", session_attributes)
        
        # 🆕 DETECCIÓN DE SEDES + CLASES CON NORMALIZACIÓN
        sedes_conocidas = [
            "normandia", "chico", "cali", "centro mayor", "chapinero", "zona rosa", 
            "unicentro", "santafe", "carrera 15", "cedritos", "suba", "hayuelos",
            "portal", "buenavista", "outlet", "tunal", "americas", "tintal",
            "country", "country club", "club country",  # ✅ AGREGAR ESTAS LÍNEAS
            "barranquilla", "cartagena", "monteria", "valledupar", "santa marta",
            "bucaramanga", "cucuta", "pereira", "manizales", "armenia", "ibague",
            "neiva", "pasto", "popayan", "villavicencio"
        ]
        
        # 🆕 NORMALIZAR TAMBIÉN LAS SEDES EN EL INPUT
        input_normalizado_sedes = corregir_sedes_comunes(input_normalizado)
        
        # 🆕 DETECCIÓN ESPECÍFICA PARA CONSULTAS DE HORARIOS DE SEDE
        palabras_horarios_sede = ["horarios", "horario", "atencion", "atención", "abren", "cierran", "abre", "cierra"]
        
        # Detectar patrón: "horarios" + "en" + sede → ConsultarSedes
        if any(palabra in input_lower for palabra in palabras_horarios_sede):
            if " en " in input_normalizado_sedes:
                partes = input_normalizado_sedes.split(" en ")
                if len(partes) >= 2:
                    parte_sede = partes[1].strip()
                    # Verificar si la segunda parte contiene una sede conocida
                    for sede in sedes_conocidas:
                        if sede in parte_sede:
                            print(f"✅ Detección local horarios de sede: '{sede}' → ConsultarSedes")
                            session_attributes["input_original_menu"] = input_transcript
                            return redirigir_a_intencion("ConsultarSedes", session_attributes)
        
        # Detectar patrón: clase + "en" + sede (con normalización)
        if " en " in input_normalizado_sedes:
            partes = input_normalizado_sedes.split(" en ")
            if len(partes) >= 2:
                parte_clase = partes[0].strip()
                parte_sede = partes[1].strip()
                
                # Verificar si la primera parte contiene una clase
                for clase in clases_conocidas:
                    if clase in parte_clase:
                        # Verificar si la segunda parte contiene una sede
                        for sede in sedes_conocidas:
                            if sede in parte_sede:
                                print(f"✅ Detección local sede+clase (normalizado): '{clase}' en '{sede}' → ConsultaGrupales")
                                session_attributes["input_original_menu"] = input_transcript
                                return redirigir_a_intencion("ConsultaGrupales", session_attributes)
        
        # Si no hay detección local, usar Bedrock con prompt mejorado
        print(f"🔍 No se detectó localmente, enviando a Bedrock: '{input_transcript}'")
        
        # 🆕 MEJORAR EL PROMPT DE BEDROCK CON EJEMPLOS DE NORMALIZACIÓN
        try:
            from respuestas import consultar_bedrock_generacion
            
            prompt = f"""
            Analiza el siguiente texto del usuario y determina si corresponde claramente a alguna de estas intenciones específicas.
            El texto puede contener errores de tipeo que debes interpretar inteligentemente.

            Intenciones disponibles:
            - FQABodytech: Preguntas frecuentes sobre Bodytech, horarios de atención, servicios, políticas del gimnasio
            - ConsultarSedes: Consultar sedes específicas, ubicaciones, horarios de sedes
            - ConsultaGrupales: Clases grupales como yoga, pilates, zumba, spinning, rumba, danza, aqua, funcional, crossfit, bodypump, bodycombat, bodybalance, horarios de clases específicas, consultas sobre actividades físicas en sedes
            - ConsultaInfoPlan: Información del plan del usuario, estado del plan, vencimientos
            - ConsultarInvitados: Consultar invitados del usuario, políticas de invitados
            - FQAReferidos: Información sobre referidos, programa de referidos
            - ConsultaIncapacidades: Consultar incapacidades médicas registradas
            - Venta: Información de ventas, planes disponibles, precios, promociones
            - CongelarPlan: Información sobre congelaciones de plan, pausar membresía

            Texto del usuario: "{input_transcript}"

            REGLAS DE CLASIFICACIÓN:
            - Si menciona CLASES ESPECÍFICAS (yoga, pilates, zumba, spinning, rumba, danza, aqua, funcional, etc.) → ConsultaGrupales
            - Si menciona HORARIOS DE CLASES o HORARIOS DE ACTIVIDADES → ConsultaGrupales  
            - Si menciona SEDE + CLASE (ej: "rumba en normandia") → ConsultaGrupales
            - Si menciona HORARIOS + NOMBRE DE SEDE (sin clase específica) → ConsultarSedes
            - Si menciona solo HORARIOS + SEDE (ej: "que horarios tienen en normandia") → ConsultarSedes
            - Si es texto aleatorio o sin contexto → NO_VALIDO

            EJEMPLOS CON ERRORES DE TIPEO:
            - "que horarios de rumbas tienen en normandie" → ConsultaGrupales (rumba + normandia)
            - "yga en chiko" → ConsultaGrupales (yoga + chico)
            - "horarios piltes centro maior" → ConsultaGrupales (pilates + centro mayor)
            - "que horarios tienen en normandia" → ConsultarSedes (horarios generales de sede)
            - "horarios de atencion en chapinero" → ConsultarSedes (horarios de sede)
            - "sedes en medelin" → ConsultarSedes (medellin)
            - "cuando es spining" → ConsultaGrupales (spinning)

            Responde SOLO con el nombre de la intención o "NO_VALIDO"
            """
            
            respuesta_bedrock = consultar_bedrock_generacion(prompt)
            intent_clasificado = respuesta_bedrock.strip()
            
            print(f"🔍 Respuesta de Bedrock: '{intent_clasificado}'")
            
            # Validar que la intención clasificada sea válida
            intenciones_validas = list(opciones_menu.values())
            
            if intent_clasificado in intenciones_validas:
                print(f"✅ Bedrock clasificó como: {intent_clasificado}")
                
                # Configurar session_attributes con el input original para contexto
                session_attributes["input_original_menu"] = input_transcript
                print(f"🔍 DEBUG: Guardando input_original_menu = '{input_transcript}'")
                
                resultado_redireccion = redirigir_a_intencion(intent_clasificado, session_attributes)
                
                # ✅ MANEJAR EL CASO CUANDO RETORNA None
                if resultado_redireccion is None:
                    print("🔄 Redireccion retornó None - necesita procesar en lambda_handler")
                    # Retornar datos para que lambda_handler continúe el flujo
                    return {
                        "continuar_flujo": True,
                        "intent_name": intent_clasificado,
                        "session_attributes": session_attributes
                    }
                else:
                    # Si la redirección retorna una respuesta, enviarla
                    return resultado_redireccion
            else:
                print(f"⚠️ Bedrock respondió: {intent_clasificado} - Mostrando menú nuevamente")
                raise ValueError("Intención no válida o input sin sentido")
                
        except Exception as e:
            print(f"❌ Error clasificando con Bedrock: {e}")
            
            # Fallback: Mostrar menú nuevamente con sugerencia
            session_attributes["esperando_seleccion_menu"] = "true"
            
            return {
                "sessionState": {
                    "dialogAction": {"type": "ElicitIntent"},
                    "sessionAttributes": session_attributes
                },
                "messages": [{
                    "contentType": "PlainText",
                    "content": (
                        f"🤔 No logré entender tu solicitud: '{input_transcript}'\n\n"
                        "Puedes usar:\n"
                        "• **Números del 1 al 9** para opciones específicas\n"
                        "• **Texto claro** como 'horarios de yoga' o 'info de mi plan'\n"
                        "• **N** para finalizar\n\n"
                        "💬 ¿Qué te gustaría hacer?"
                    )
                }]
            }
def redirigir_a_intencion(intent_clasificado, session_attributes):
    
    """
    Redirige a la intención clasificada manteniendo el contexto del input original
    """
    input_original = session_attributes.get("input_original_menu", "")
    
    # Limpiar banderas del menú
    session_attributes.pop("esperando_seleccion_menu", None)
    session_attributes.pop("en_flujo_activo", None)  # Limpiar flujo anterior
    
    # ConsultaGrupales - procesar directamente con input original
    if intent_clasificado == "ConsultaGrupales" and input_original:
        print(f"🎯 Procesando ConsultaGrupales directamente con input original: '{input_original}'")
        
        # Importar la función necesaria
        from services import extraer_y_validar_slots_grupales
        
        # Procesar directamente el input original
        resultado = extraer_y_validar_slots_grupales(input_original, session_attributes, {
            "name": "ConsultaGrupales",
            "slots": {}
        })
        
        print(f"🔍 Resultado de extracción directa: {resultado}")
        
        # Si la función devuelve una respuesta directa, enviarla
        if resultado and resultado.get("sessionState"):
            return resultado
        
        # Si hay error, enviar el error
        if resultado and resultado.get("error"):
            return resultado["error"]
        
        # Si se extrajeron parámetros, configurar para continuar el flujo
        if (resultado 
            and not resultado.get("error")
            and (resultado.get("ciudad_id") or resultado.get("sede_id") or resultado.get("clase_id"))):
            
            print("✅ Parámetros extraídos correctamente del input original!")
            
            # Actualizar session_attributes con los datos detectados
            if resultado.get("ciudad_id"):
                session_attributes["ciudad_id"] = str(resultado["ciudad_id"])
                session_attributes["ciudad_nombre"] = resultado["ciudad_nombre"]
            
            if resultado.get("sede_id"):
                session_attributes["sede_id"] = str(resultado["sede_id"])
                session_attributes["sede_nombre"] = resultado["sede_nombre"]
            
            if resultado.get("clase_id"):
                session_attributes["clase_id"] = str(resultado["clase_id"])
                session_attributes["clase_nombre"] = resultado["clase_nombre"]
            
            if resultado.get("fecha"):
                session_attributes["fecha_temporal"] = resultado["fecha"]
            
            # ✅ CRUCIAL: NO CREAR RESPUESTA ESTÁTICA - DEJAR QUE CONTINÚE EL FLUJO
            session_attributes["procesar_con_datos_extraidos"] = "true"
            session_attributes["input_original_menu"] = input_original
            
            # ✅ RETORNAR ESTRUCTURA PARA CONTINUAR EL FLUJO
            print("🔄 Continuando al lambda_handler para mostrar sedes...")
            return {
                "continuar_flujo": True,
                "intent_name": "ConsultaGrupales",
                "session_attributes": session_attributes
            }
    
    elif intent_clasificado == "ConsultarSedes":
        print("🔍 Procesando ConsultarSedes con extracción automática...")
        
        # Procesar directamente el input original
        from services import extraer_y_validar_slots_sedes
        resultado = extraer_y_validar_slots_sedes(input_original, session_attributes, {
            "name": "ConsultarSedes",
            "slots": {}
        })
        
        print(f"🔍 Resultado de extracción sedes: {resultado}")
        
        # Si la función devuelve una respuesta directa, enviarla
        if resultado and resultado.get("sessionState"):
            return resultado
        
        # Si hay error, enviar el error
        if resultado and resultado.get("error"):
            return resultado["error"]
        
        # Si se extrajeron parámetros, configurar para continuar el flujo
        if (resultado 
            and not resultado.get("error")
            and (resultado.get("ciudad_id") or resultado.get("sede_id") or resultado.get("categoria_nombre"))):
            
            print("✅ Parámetros de sedes extraídos correctamente del input original!")
            
            # Actualizar session_attributes con los datos detectados
            if resultado.get("ciudad_id"):
                session_attributes["ciudad_id"] = str(resultado["ciudad_id"])
                session_attributes["ciudad_nombre"] = resultado["ciudad_nombre"]
            
            if resultado.get("sede_id"):
                session_attributes["sede_id"] = str(resultado["sede_id"])
                session_attributes["sede_nombre"] = resultado["sede_nombre"]
            
            if resultado.get("categoria_nombre"):
                session_attributes["categoria_detectada"] = resultado["categoria_nombre"]
            
            if resultado.get("tipo_consulta"):
                session_attributes["tipo_consulta_temporal"] = resultado["tipo_consulta"]
            
            # ✅ CRUCIAL: NO CREAR RESPUESTA ESTÁTICA - DEJAR QUE CONTINÚE EL FLUJO
            session_attributes["procesar_con_datos_extraidos"] = "true"
            session_attributes["input_original_menu"] = input_original
            
            # ✅ RETORNAR ESTRUCTURA PARA CONTINUAR EL FLUJO
            print("🔄 Continuando al lambda_handler para mostrar sedes de ConsultarSedes...")
            return {
                "continuar_flujo": True,
                "intent_name": "ConsultarSedes",
                "session_attributes": session_attributes
            }
    
    # Para otras intenciones o ConsultaGrupales sin datos extraídos, usar flujo normal
    intents_requieren_doc = {"ConsultaInfoPlan", "ConsultarInvitados", "ConsultaIncapacidades", "FQAReferidos", "CongelarPlan"}
    
    if intent_clasificado in intents_requieren_doc:
        intenciones_con_documento = session_attributes.get("intenciones_con_documento", "")
        intenciones_set = set(intenciones_con_documento.split(",")) if intenciones_con_documento else set()
        
        # Agregar la nueva intención al conjunto
        intenciones_set.add(intent_clasificado)
        session_attributes["intenciones_con_documento"] = ",".join(intenciones_set)
        
        # ✅ Verificar si YA HAY un documento registrado previamente
        tiene_documento_previo = (
            session_attributes.get("document_type_id") and 
            session_attributes.get("document_number")
        )
        
        print(f"🔍 ===== DEBUG VALIDACIÓN DOCUMENTO PREVIO (MENÚ) =====")
        print(f"🔍 Intent actual: {intent_clasificado}")
        print(f"🔍 intenciones_set: {intenciones_set}")
        print(f"🔍 Longitud intenciones_set: {len(intenciones_set)}")
        print(f"🔍 Tiene documento previo: {tiene_documento_previo}")
        print(f"🔍 document_type_id: {session_attributes.get('document_type_id')}")
        print(f"🔍 document_number: {session_attributes.get('document_number')}")
        print(f"🔍 preguntando_otro_documento: {session_attributes.get('preguntando_otro_documento')}")
        print(f"🔍 cambiando_documento: {session_attributes.get('cambiando_documento')}")
        print(f"🔍 =====================================================")
        
        # Si ya tiene documento previo Y está seleccionando nueva intención
        if (
            tiene_documento_previo
            and not session_attributes.get("preguntando_otro_documento")
            and not session_attributes.get("cambiando_documento")
            and session_attributes.get("acepto_politicas") == "true"
        ):
            print(f"🔍 ✅ ACTIVANDO pregunta de otro documento para {intent_clasificado} desde menú")
            session_attributes["preguntando_otro_documento"] = "true"
            session_attributes["cambiando_documento"] = ""
            session_attributes["intencion_tras_documento"] = intent_clasificado  # ✅ AGREGAR ESTA LÍNEA
            session_attributes["en_flujo_activo"] = intent_clasificado  # ✅ Establecer flujo activo
            
            return {
                "sessionState": {
                    "dialogAction": {"type": "ElicitSlot", "slotToElicit": "cambiar_documento"},
                    "intent": {
                        "name": intent_clasificado,
                        "state": "InProgress",
                        "slots": {}
                    },
                    "sessionAttributes": session_attributes
                },
                "messages": [{
                    "contentType": "PlainText",
                    "content": "¿Quieres consultar con otro documento o seguir usando el que ya indicaste?\n\n"
                    "💬 **Puedes Decirme:** 'Otro documento' o 'Mismo documento'\n\n"
                }]
            }
    
    # Limpiar datos de sesión pero mantener algunos básicos
    datos_basicos = {
        "acepto_politicas": session_attributes.get("acepto_politicas"),
        "document_type_id": session_attributes.get("document_type_id"),
        "document_type_raw": session_attributes.get("document_type_raw"),
        "document_number": session_attributes.get("document_number"),
        "intenciones_con_documento": session_attributes.get("intenciones_con_documento"),
        "input_original_menu": session_attributes.get("input_original_menu")  # ✅ PRESERVAR input original
    }
    
    # Limpiar session_attributes manteniendo solo datos básicos
    session_attributes.clear()
    for k, v in datos_basicos.items():
        if v is not None:
            session_attributes[k] = v
    
    # Configuraciones específicas por intención
    if intent_clasificado == "ConsultarSedes":
        # 🆕 Procesar input original para extraer parámetros automáticamente
        input_original = session_attributes.get("input_original_menu", "")
        
        print(f"🔍 DEBUG ConsultarSedes: input_original = '{input_original}'")
        print(f"🔍 DEBUG ConsultarSedes: session_attributes keys = {list(session_attributes.keys())}")
        
        if input_original:
            print(f"🎯 Procesando ConsultarSedes con input original: '{input_original}'")
            
            resultado_automatico = procesar_input_original_sedes(input_original, session_attributes)
            
            # Si el procesamiento automático fue exitoso, devolverlo
            if resultado_automatico:
                return resultado_automatico
        
        # Si no hay input original o no se pudo procesar automáticamente, flujo normal
        return {
            "sessionState": {
                "dialogAction": {"type": "ElicitSlot", "slotToElicit": "ciudad"},
                "intent": {
                    "name": intent_clasificado,
                    "slots": {},
                    "state": "InProgress"
                },
                "sessionAttributes": session_attributes
            },
            "messages": [{
                "contentType": "PlainText",
                "content": "¡Perfecto! Te ayudo a consultar información de sedes y horarios 🏢\n\n¿En qué ciudad deseas consultar las sedes?"
            }]
        }
    
    elif intent_clasificado == "ConsultaGrupales":
        return {
            "sessionState": {
                "dialogAction": {"type": "ElicitSlot", "slotToElicit": "ciudad"},
                "intent": {
                    "name": intent_clasificado,
                    "slots": {},
                    "state": "InProgress"
                },
                "sessionAttributes": session_attributes
            },
            "messages": [{
                "contentType": "PlainText",
                "content": "¡Excelente! Te ayudo a consultar clases grupales 🏃‍♂️\n\n¿En qué ciudad deseas consultar las clases?"
            }]
        }
    
    elif intent_clasificado == "ConsultaInfoPlan":
        # Configurar flujo activo
        session_attributes["en_flujo_activo"] = intent_clasificado
        
        from services import validar_documento_usuario
        
        # Crear intent mock para validar_documento_usuario
        intent_mock = {
            "name": intent_clasificado,
            "slots": {},
            "state": "InProgress"
        }
        
        # Llamar a validar_documento_usuario que maneja todo el flujo
        document_type_id, document_number, session_attributes, respuesta_incompleta = validar_documento_usuario(
            {}, session_attributes, "", intent_mock
        )
        
        # Si necesita más datos, retornar la respuesta de validación
        if respuesta_incompleta:
            return respuesta_incompleta
        
        
        # Si ya tiene todos los datos, continuar con el flujo normal de la intención
        return {
            "sessionState": {
                "dialogAction": {"type": "ElicitIntent"},
                "intent": {
                    "name": intent_clasificado,
                    "slots": {},
                    "state": "InProgress"
                },
                "sessionAttributes": session_attributes
            },
            "messages": [{
                "contentType": "PlainText",
                "content": "¡Perfecto! Consultando información de tu plan 📅"
            }]
        }
    
    elif intent_clasificado in ["ConsultarInvitados", "FQAReferidos", "ConsultaIncapacidades", "CongelarPlan"]:
        # Configurar flujo activo
        session_attributes["en_flujo_activo"] = intent_clasificado
        
        from services import validar_documento_usuario
        
        # Crear intent mock para validar_documento_usuario
        intent_mock = {
            "name": intent_clasificado,
            "slots": {},
            "state": "InProgress"
        }
        
        # Llamar a validar_documento_usuario que maneja todo el flujo
        document_type_id, document_number, session_attributes, respuesta_incompleta = validar_documento_usuario(
            {}, session_attributes, "", intent_mock
        )
        
        # Si necesita más datos, retornar la respuesta de validación
        if respuesta_incompleta:
            return respuesta_incompleta
        
        # Si ya tiene todos los datos, continuar con el flujo normal de la intención
        mensajes_cuando_tiene_datos = {
            "ConsultarInvitados": "¡Perfecto! Consultando tus invitados 👥",
            "FQAReferidos": "¡Excelente! Consultando información de tus referidos 🏆", 
            "ConsultaIncapacidades": "¡Perfecto! Consultando tus incapacidades 🧾",
            "CongelarPlan": "¡Entendido! Consultando información sobre congelaciones ❄️"
        }
        
        return {
            "sessionState": {
                "dialogAction": {"type": "ElicitIntent"},
                "intent": {
                    "name": intent_clasificado,
                    "slots": {},
                    "state": "InProgress"
                },
                "sessionAttributes": session_attributes
            },
            "messages": [{
                "contentType": "PlainText",
                "content": mensajes_cuando_tiene_datos.get(intent_clasificado, f"¡Perfecto! Procesando tu consulta 📋")
            }]
        }
    
    # Para FQABodytech y Venta - intenciones que NO requieren documento
    else:
        # Configurar flujo activo para todas las intenciones
        session_attributes["en_flujo_activo"] = intent_clasificado
        
        mensajes_bienvenida = {
            "FQABodytech": "¡Excelente! Te ayudo con preguntas frecuentes sobre Bodytech 🏋️‍♂️\n\n¿Qué información específica necesitas? Por ejemplo:\n• Horarios de atención\n• Servicios disponibles\n• Políticas del gimnasio\n• Información sobre clases\n\n💬 **Escribe tu pregunta:**",
            "Venta": "¡Perfecto! Te ayudo con información de ventas 🛍️\n\n¿Qué información de ventas necesitas? Por ejemplo:\n• Planes disponibles\n• Precios y promociones\n• Servicios adicionales\n• Métodos de pago\n\n💬 **Escribe tu consulta:**"
        }
        
        return {
            "sessionState": {
                "dialogAction": {"type": "ElicitIntent"},
                "intent": {
                    "name": intent_clasificado,
                    "slots": {},
                    "state": "InProgress"
                },
                "sessionAttributes": session_attributes
            },
            "messages": [{
                "contentType": "PlainText",
                "content": mensajes_bienvenida.get(intent_clasificado, f"¡Perfecto! ¿En qué puedo ayudarte con {intent_clasificado}?")
            }]
        }

def _procesar_otra_fecha(session_attributes):
    """Procesa transición a otra fecha manteniendo ciudad, sede y clase"""
    print("✅ Transición: OTRA FECHA")
    
    ciudad_actual = session_attributes.get("ciudad_nombre") or session_attributes.get("ciudad")
    sede_actual = session_attributes.get("sede_nombre")
    clase_actual = session_attributes.get("clase_nombre")
    
    if not ciudad_actual or not sede_actual:
        print("❌ Faltan datos de ciudad o sede para otra fecha")
        return {
            "sessionState": {
                "dialogAction": {"type": "ElicitSlot", "slotToElicit": "ciudad"},
                "intent": {"name": "ConsultaGrupales", "state": "InProgress", "slots": {}},
                "sessionAttributes": session_attributes
            },
            "messages": [{"contentType": "PlainText", "content": "¿En qué ciudad y sede deseas consultar otra fecha?"}]
        }
    
    # Limpiar solo datos específicos de fecha, mantener todo lo demás
    keys_to_remove = ["fecha", "esperando_transicion_grupales"]
    for key in keys_to_remove:
        session_attributes.pop(key, None)
    
    # Configurar slots manteniendo ciudad, sede y clase (si existe)
    slots_nuevos = {
        "ciudad": {
            "value": {
                "originalValue": ciudad_actual,
                "resolvedValues": [ciudad_actual],
                "interpretedValue": ciudad_actual
            },
            "shape": "Scalar"
        },
        "sede": {
            "value": {
                "originalValue": sede_actual,
                "resolvedValues": [sede_actual],
                "interpretedValue": sede_actual
            },
            "shape": "Scalar"
        }
    }
    
    # Si hay clase específica, mantenerla también
    if clase_actual:
        slots_nuevos["clase"] = {
            "value": {
                "originalValue": clase_actual,
                "resolvedValues": [clase_actual],
                "interpretedValue": clase_actual
            },
            "shape": "Scalar"
        }
        mensaje_fecha = f"¡Perfecto! Te ayudo a consultar {clase_actual} en la sede {sede_actual} ({ciudad_actual}) para otra fecha. 📅"
    else:
        mensaje_fecha = f"¡Perfecto! Te ayudo a consultar las clases en la sede {sede_actual} ({ciudad_actual}) para otra fecha. 📅"
    
    # Marcar que estamos en flujo activo
    session_attributes["en_flujo_activo"] = "ConsultaGrupales"
    
    print(f"✅ Parámetros mantenidos - Ciudad: {ciudad_actual}, Sede: {sede_actual}, Clase: {clase_actual or 'Todas'}")
    
    return {
        "sessionState": {
            "dialogAction": {"type": "ElicitSlot", "slotToElicit": "fecha"},
            "intent": {
                "name": "ConsultaGrupales",
                "slots": slots_nuevos,
                "state": "InProgress"
            },
            "sessionAttributes": session_attributes
        },
        "messages": [{
            "contentType": "PlainText",
            "content": f"{mensaje_fecha}\n\n¿Para qué fecha deseas consultar? Puedes escribir:\n• YYYY-MM-DD (2025-01-15)\n• DD de MMMM (15 de enero)\n• DD/MM (15/01)\n• 'hoy' o 'mañana'"
        }]
    }
    print("✅ Transición: OTRA FECHA")
    
    ciudad_actual = session_attributes.get("ciudad_nombre") or session_attributes.get("ciudad")
    sede_actual = session_attributes.get("sede_nombre")
    clase_actual = session_attributes.get("clase_nombre")
    
    if not ciudad_actual or not sede_actual:
        print("❌ Faltan datos de ciudad o sede para otra fecha")
        return {
            "sessionState": {
                "dialogAction": {"type": "ElicitSlot", "slotToElicit": "ciudad"},
                "intent": {"name": "ConsultaGrupales", "state": "InProgress", "slots": {}},
                "sessionAttributes": session_attributes
            },
            "messages": [{"contentType": "PlainText", "content": "¿En qué ciudad y sede deseas consultar otra fecha?"}]
        }
    
    # Limpiar solo datos específicos de fecha, mantener todo lo demás
    keys_to_remove = ["fecha", "esperando_transicion_grupales"]
    for key in keys_to_remove:
        session_attributes.pop(key, None)
    
    # Configurar slots manteniendo ciudad, sede y clase (si existe)
    slots_nuevos = {
        "ciudad": {
            "value": {
                "originalValue": ciudad_actual,
                "resolvedValues": [ciudad_actual],
                "interpretedValue": ciudad_actual
            },
            "shape": "Scalar"
        },
        "sede": {
            "value": {
                "originalValue": sede_actual,
                "resolvedValues": [sede_actual],
                "interpretedValue": sede_actual
            },
            "shape": "Scalar"
        }
    }
    
    # Si hay clase específica, mantenerla también
    if clase_actual:
        slots_nuevos["clase"] = {
            "value": {
                "originalValue": clase_actual,
                "resolvedValues": [clase_actual],
                "interpretedValue": clase_actual
            },
            "shape": "Scalar"
        }
        mensaje_fecha = f"¡Perfecto! Te ayudo a consultar {clase_actual} en la sede {sede_actual} ({ciudad_actual}) para otra fecha. 📅"
    else:
        mensaje_fecha = f"¡Perfecto! Te ayudo a consultar las clases en la sede {sede_actual} ({ciudad_actual}) para otra fecha. 📅"
    
    # Marcar que estamos en flujo activo
    session_attributes["en_flujo_activo"] = "ConsultaGrupales"
    
    print(f"✅ Parámetros mantenidos - Ciudad: {ciudad_actual}, Sede: {sede_actual}, Clase: {clase_actual or 'Todas'}")
    
    return {
        "sessionState": {
            "dialogAction": {"type": "ElicitSlot", "slotToElicit": "fecha"},
            "intent": {
                "name": "ConsultaGrupales",
                "slots": slots_nuevos,
                "state": "InProgress"
            },
            "sessionAttributes": session_attributes
        },
        "messages": [{
            "contentType": "PlainText",
            "content": f"{mensaje_fecha}\n\n¿Para qué fecha deseas consultar? Puedes escribir:\n• YYYY-MM-DD (2025-01-15)\n• DD de MMMM (15 de enero)\n• DD/MM (15/01)\n• 'hoy' o 'mañana'"
        }]
    }

#####################################
# Manejar logica de transicion asesor
####################################

def incrementar_contador_no_reconocidas(session_attributes):
    """Incrementa el contador de entradas no reconocidas"""
    contador_actual = int(session_attributes.get("entradas_no_reconocidas", 0))
    contador_nuevo = contador_actual + 1
    session_attributes["entradas_no_reconocidas"] = str(contador_nuevo)
    
    print(f"🔢 Contador entradas no reconocidas: {contador_nuevo}")
    return contador_nuevo

def resetear_contador_no_reconocidas(session_attributes):
    """Resetea el contador de entradas no reconocidas"""
    session_attributes.pop("entradas_no_reconocidas", None)
    session_attributes.pop("esperando_respuesta_asesor", None)
    print("✅ Contador de entradas no reconocidas reseteado")

def debe_ofrecer_asesor(session_attributes):
    """Verifica si debe ofrecer hablar con asesor"""
    contador = int(session_attributes.get("entradas_no_reconocidas", 0))
    return contador >= 2

def ofrecer_hablar_con_asesor(session_attributes):
    """Ofrece la opción de hablar con un asesor"""
    session_attributes["esperando_respuesta_asesor"] = "true"
    
    return {
        "sessionState": {
            "dialogAction": {"type": "ElicitIntent"},
            "sessionAttributes": session_attributes
        },
        "messages": [{
            "contentType": "PlainText",
            "content": (
                "🤔 Veo que no he logrado entender tus últimas consultas.\n\n"
                "¿Te gustaría hablar con uno de nuestros asesores para recibir ayuda personalizada?\n\n"
                "💬 Responde:\n"
                "• **'Sí'** - Para conectarte con un asesor\n"
                "• **'No'** - Para volver al menú principal\n\n"
                "¿Qué prefieres?"
            )
        }]
    }

def procesar_respuesta_asesor(input_transcript, session_attributes):
    """Procesa la respuesta del usuario sobre hablar con asesor"""
    if session_attributes.get("esperando_respuesta_asesor") != "true":
        return None
    
    input_lower = input_transcript.lower().strip()
    print(f"🔍 Procesando respuesta asesor: '{input_lower}'")
    
    # Limpiar bandera
    session_attributes.pop("esperando_respuesta_asesor", None)
    resetear_contador_no_reconocidas(session_attributes)
    
    # Respuestas afirmativas - solicitar calificación ANTES de pasar a asesor
    if any(palabra in input_lower for palabra in [
        "si", "sí", "yes", "claro", "vale", "ok", "por supuesto", 
        "quiero", "necesito", "asesor", "ayuda"
    ]):
        print("✅ Usuario quiere hablar con asesor - solicitando calificación primero")
        
        # 🆕 MARCAR QUE DESPUÉS DE CALIFICAR VA A ASESOR
        session_attributes["despues_calificacion_asesor"] = "true"
        session_attributes["esperando_calificacion"] = "true"
        
        return {
            "sessionState": {
                "dialogAction": {"type": "ElicitIntent"},
                "sessionAttributes": session_attributes
            },
            "messages": [{
                "contentType": "PlainText",
                "content": (
                    "¡Perfecto! Antes de conectarte con un asesor, nos gustaría conocer tu experiencia con el asistente virtual.\n\n"
                    "¿Podrías calificar tu experiencia?\n\n"
                    "⭐ 1 estrella - Muy mala\n"
                    "⭐⭐ 2 estrellas - Mala\n"
                    "⭐⭐⭐ 3 estrellas - Regular\n"
                    "⭐⭐⭐⭐ 4 estrellas - Buena\n"
                    "⭐⭐⭐⭐⭐ 5 estrellas - Excelente\n\n"
                    "💬 **Responde con un número del 1 al 5:**"
                )
            }]
        }
    
    # Respuestas negativas - volver al menú principal
    elif any(palabra in input_lower for palabra in [
        "no", "nada", "gracias", "menu", "menú", "principal", "no gracias"
    ]):
        print("✅ Usuario no quiere asesor - volviendo al menú principal")
        return mostrar_menu_principal(session_attributes)
    
    # Respuesta no clara - preguntar de nuevo
    else:
        print(f"❌ Respuesta no clara sobre asesor: '{input_transcript}'")
        session_attributes["esperando_respuesta_asesor"] = "true"
        
        return {
            "sessionState": {
                "dialogAction": {"type": "ElicitIntent"},
                "sessionAttributes": session_attributes
            },
            "messages": [{
                "contentType": "PlainText",
                "content": (
                    "🤔 No entendí tu respuesta.\n\n"
                    "¿Quieres hablar con un asesor?\n\n"
                    "💬 Responde solo:\n"
                    "• **'Sí'** - Para conectarte con un asesor\n"
                    "• **'No'** - Para volver al menú principal"
                )
            }]
        }

###############################
# Verificar si es input valido
###############################

def es_input_valido(input_transcript):
    """
    Valida si el input del usuario es texto coherente o solo caracteres sin sentido
    """
    
    # Limpiar el input
    texto_limpio = input_transcript.strip().lower()
    
    # 1. Si está vacío, es inválido
    if not texto_limpio:
        return False
    
    # ✅ NUEVO: Si es solo números (posible documento), es válido
    if texto_limpio.isdigit() and 4 <= len(texto_limpio) <= 15:
        print(f"✅ Input válido (número de documento): '{texto_limpio}'")
        return True
    
    # 2. Si es solo números largos sin sentido (más de 15 dígitos)
    if texto_limpio.isdigit() and len(texto_limpio) > 15:
        return False
    
    # 3. Si tiene más del 70% de caracteres repetidos o sin sentido
    caracteres_unicos = len(set(texto_limpio.replace(" ", "")))
    total_caracteres = len(texto_limpio.replace(" ", ""))
    
    if total_caracteres > 5 and caracteres_unicos / total_caracteres < 0.3:
        print(f"🚫 Texto con pocos caracteres únicos: {caracteres_unicos}/{total_caracteres}")
        return False
    
    # 4. ✅ MEJORAR: Detectar patrones de tecleo aleatorio (pero excluir palabras válidas)
    # Lista de palabras válidas que pueden tener muchas consonantes
    palabras_validas_consonantes = [
        "country", "centro", "rumba", "spinning", "crossfit", "strength", "strong",
        "chapinero", "normandia", "outlet", "portal", "tintal", "hayuelos", "cedritos",
        "horarios", "tienen", "grupales", "clases", "horario", "consultar"
    ]
    
    # Solo aplicar filtro de consonantes si NO contiene palabras válidas conocidas
    contiene_palabra_valida = any(palabra in texto_limpio for palabra in palabras_validas_consonantes)
    
    if not contiene_palabra_valida:
        patron_sin_sentido = re.compile(r'[bcdfghjklmnpqrstvwxyz]{6,}')  # Aumentar umbral a 6
        if patron_sin_sentido.search(texto_limpio):
            print(f"🚫 Patrón de consonantes detectado: {texto_limpio}")
            return False
    
    # 5. Detectar secuencias de teclado obvias
    secuencias_teclado = [
        'qwerty', 'asdf', 'zxcv', 'qaz', 'wsx', 'edc', 'rfv', 'tgb', 'yhn', 'ujm',
        'qlllq', 'asklj', 'lkjh', 'mnbv', 'poiu', 'wert', 'dfgh', 'cvbn'
    ]
    
    for secuencia in secuencias_teclado:
        if secuencia in texto_limpio:
            print(f"🚫 Secuencia de teclado detectada: {secuencia}")
            return False
    
    # 6. Si es muy corto pero no tiene sentido (menos de 3 caracteres válidos)
    if len(texto_limpio.replace(" ", "")) < 3 and not any(palabra in texto_limpio for palabra in [
        "si", "no", "ok", "hola", "bye", "m", "n", "1", "2", "3", "4", "5", "6", "7", "8", "9"
    ]):
        return False
    
    # 7. Detectar si NO tiene ninguna vocal (excepto números y palabras muy cortas)
    if (len(texto_limpio) > 2 and 
        not re.search(r'[aeiouáéíóú]', texto_limpio) and 
        not texto_limpio.isdigit()):  # ✅ AGREGAR esta condición
        print(f"🚫 Texto sin vocales: {texto_limpio}")
        return False
    
    print(f"✅ Input válido: '{texto_limpio}'")
    return True#####################################
# Manejar logica de transicion asesor
####################################

def incrementar_contador_no_reconocidas(session_attributes):
    """Incrementa el contador de entradas no reconocidas"""
    contador_actual = int(session_attributes.get("entradas_no_reconocidas", 0))
    contador_nuevo = contador_actual + 1
    session_attributes["entradas_no_reconocidas"] = str(contador_nuevo)
    
    print(f"🔢 Contador entradas no reconocidas: {contador_nuevo}")
    return contador_nuevo

def resetear_contador_no_reconocidas(session_attributes):
    """Resetea el contador de entradas no reconocidas"""
    session_attributes.pop("entradas_no_reconocidas", None)
    session_attributes.pop("esperando_respuesta_asesor", None)
    print("✅ Contador de entradas no reconocidas reseteado")

def debe_ofrecer_asesor(session_attributes):
    """Verifica si debe ofrecer hablar con asesor"""
    contador = int(session_attributes.get("entradas_no_reconocidas", 0))
    return contador >= 2

def ofrecer_hablar_con_asesor(session_attributes):
    """Ofrece la opción de hablar con un asesor"""
    session_attributes["esperando_respuesta_asesor"] = "true"
    
    return {
        "sessionState": {
            "dialogAction": {"type": "ElicitIntent"},
            "sessionAttributes": session_attributes
        },
        "messages": [{
            "contentType": "PlainText",
            "content": (
                "🤔 Veo que no he logrado entender tus últimas consultas.\n\n"
                "¿Te gustaría hablar con uno de nuestros asesores para recibir ayuda personalizada?\n\n"
                "💬 Responde:\n"
                "• **'Sí'** - Para conectarte con un asesor\n"
                "• **'No'** - Para volver al menú principal\n\n"
                "¿Qué prefieres?"
            )
        }]
    }

def procesar_respuesta_asesor(input_transcript, session_attributes):
    """Procesa la respuesta del usuario sobre hablar con asesor"""
    if session_attributes.get("esperando_respuesta_asesor") != "true":
        return None
    
    input_lower = input_transcript.lower().strip()
    print(f"🔍 Procesando respuesta asesor: '{input_lower}'")
    
    # Limpiar bandera
    session_attributes.pop("esperando_respuesta_asesor", None)
    resetear_contador_no_reconocidas(session_attributes)
    
    # Respuestas afirmativas - solicitar calificación ANTES de pasar a asesor
    if any(palabra in input_lower for palabra in [
        "si", "sí", "yes", "claro", "vale", "ok", "por supuesto", 
        "quiero", "necesito", "asesor", "ayuda"
    ]):
        print("✅ Usuario quiere hablar con asesor - solicitando calificación primero")
        
        # 🆕 MARCAR QUE DESPUÉS DE CALIFICAR VA A ASESOR
        session_attributes["despues_calificacion_asesor"] = "true"
        session_attributes["esperando_calificacion"] = "true"
        
        return {
            "sessionState": {
                "dialogAction": {"type": "ElicitIntent"},
                "sessionAttributes": session_attributes
            },
            "messages": [{
                "contentType": "PlainText",
                "content": (
                    "¡Perfecto! Antes de conectarte con un asesor, nos gustaría conocer tu experiencia con el asistente virtual.\n\n"
                    "¿Podrías calificar tu experiencia?\n\n"
                    "⭐ 1 estrella - Muy mala\n"
                    "⭐⭐ 2 estrellas - Mala\n"
                    "⭐⭐⭐ 3 estrellas - Regular\n"
                    "⭐⭐⭐⭐ 4 estrellas - Buena\n"
                    "⭐⭐⭐⭐⭐ 5 estrellas - Excelente\n\n"
                    "💬 **Responde con un número del 1 al 5:**"
                )
            }]
        }
    
    # Respuestas negativas - volver al menú principal
    elif any(palabra in input_lower for palabra in [
        "no", "nada", "gracias", "menu", "menú", "principal", "no gracias"
    ]):
        print("✅ Usuario no quiere asesor - volviendo al menú principal")
        return mostrar_menu_principal(session_attributes)
    
    # Respuesta no clara - preguntar de nuevo
    else:
        print(f"❌ Respuesta no clara sobre asesor: '{input_transcript}'")
        session_attributes["esperando_respuesta_asesor"] = "true"
        
        return {
            "sessionState": {
                "dialogAction": {"type": "ElicitIntent"},
                "sessionAttributes": session_attributes
            },
            "messages": [{
                "contentType": "PlainText",
                "content": (
                    "🤔 No entendí tu respuesta.\n\n"
                    "¿Quieres hablar con un asesor?\n\n"
                    "💬 Responde solo:\n"
                    "• **'Sí'** - Para conectarte con un asesor\n"
                    "• **'No'** - Para volver al menú principal"
                )
            }]
        }
