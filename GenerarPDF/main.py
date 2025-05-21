import json

def lambda_handler(event, context):
    intent_name = event['sessionState']['intent']['name']
    usuarios = [
        {'nombre': 'Carlos', 'apellido': 'Reina Ramos', 'documento': 1193037017, 'usuario': 'creina'},
        {'nombre': 'María', 'apellido': 'Gómez', 'documento': 1193037018, 'usuario': 'mgomez'},
        {'nombre': 'Lucía', 'apellido': 'Pérez', 'documento': 1193037019, 'usuario': 'lperez'},
        {'nombre': 'Andrés', 'apellido': 'Martínez', 'documento': 1193037020, 'usuario': 'amartinez'},
        {'nombre': 'Juan', 'apellido': 'López', 'documento': 1193037021, 'usuario': 'jlopez'},
        {'nombre': 'José', 'apellido': 'Ramírez', 'documento': 1193037022, 'usuario': 'jramirez'},
        {'nombre': 'Ana', 'apellido': 'Torres', 'documento': 1193037023, 'usuario': 'atorres'},
        {'nombre': 'Luis', 'apellido': 'Sánchez', 'documento': 1193037024, 'usuario': 'lsanchez'},
        {'nombre': 'Sofía', 'apellido': 'Ruiz', 'documento': 1193037025, 'usuario': 'sruiz'},
        {'nombre': 'Isabella', 'apellido': 'Morales', 'documento': 1193037026, 'usuario': 'imorales'},
        {'nombre': 'Diego', 'apellido': 'Castro', 'documento': 1193037027, 'usuario': 'dcastro'},
        {'nombre': 'Valentina', 'apellido': 'Vargas', 'documento': 1193037028, 'usuario': 'vvargas'},
        {'nombre': 'Miguel', 'apellido': 'Ortega', 'documento': 1193037029, 'usuario': 'mortega'},
        {'nombre': 'Laura', 'apellido': 'Navarro', 'documento': 1193037030, 'usuario': 'lnavarro'},
        {'nombre': 'Pablo', 'apellido': 'Mendoza', 'documento': 1193037031, 'usuario': 'pmendoza'},
        {'nombre': 'Elena', 'apellido': 'Silva', 'documento': 1193037032, 'usuario': 'esilva'},
        {'nombre': 'Javier', 'apellido': 'Rojas', 'documento': 1193037033, 'usuario': 'jrojas'},
        {'nombre': 'Carmen', 'apellido': 'Herrera', 'documento': 1193037034, 'usuario': 'cherrera'},
        {'nombre': 'David', 'apellido': 'Jiménez', 'documento': 1193037035, 'usuario': 'djimenez'},
        {'nombre': 'Patricia', 'apellido': 'Flores', 'documento': 1193037036, 'usuario': 'pflores'},
        {'nombre': 'Antonio', 'apellido': 'Gutiérrez', 'documento': 1193037037, 'usuario': 'agutierrez'},
        {'nombre': 'Cristina', 'apellido': 'Castillo', 'documento': 1193037038, 'usuario': 'ccastillo'}
    ]

    if intent_name == 'Solicitud_name':
        print(json.dumps(event))  # Para depuración en CloudWatch
        slot_nombre = event['sessionState']['intent']['slots'].get('nombre')
        if slot_nombre and slot_nombre.get('value') and slot_nombre['value'].get('interpretedValue'):
            nombre_buscado = slot_nombre['value']['interpretedValue'].strip().lower()
        else:
            nombre_buscado = ""

        if not nombre_buscado:
            respuesta = "No proporcionaste un nombre válido. Por favor, intenta de nuevo."
        else:
            encontrado = False
            for u in usuarios:
                if u['nombre'].lower() == nombre_buscado:
                    respuesta = (f"El usuario para {u['nombre']} {u['apellido']} es '{u['usuario']}' "
                                 f"y su documento es {u['documento']}.")
                    encontrado = True
                    break
            if not encontrado:
                respuesta = f"No se encontró información para el nombre '{nombre_buscado.title()}'."

    elif intent_name == 'FallbackIntent':
        respuesta = "Lo siento, no entendí lo que dijiste. ¿Podrías reformular tu pregunta?"

    else:
        respuesta = f"No tengo una respuesta para la intención '{intent_name}'."

    return {
        "sessionState": {
            "dialogAction": {
                "type": "Close"
            },
            "intent": {
                "name": intent_name,
                "state": "Fulfilled"
            }
        },
        "messages": [
            {
                "contentType": "PlainText",
                "content": respuesta
            }
        ]
    }