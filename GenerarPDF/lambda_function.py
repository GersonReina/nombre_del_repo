import json
import psycopg2
import pandas as pd

def consultar_usuarios(document_number=None):
    try:
        conn = psycopg2.connect(
            host='dataanalytics-dhw.cw69ipuk8wdc.us-east-1.redshift.amazonaws.com',
            port=5439,
            dbname='dev',
            user='admindata',
            password='Data4nalyticS**',
            options="-c client_encoding=UTF8"
        )
        with conn.cursor() as cur:
            if document_number:
                cur.execute("""
                    select *
                    from my_bodytech.members m 
                    where document_number = %s
                    limit 5;
                """, (document_number,))
            else:
                cur.execute("""
                    select *
                    from my_bodytech.members m 
                    limit 5;
                """)
            resultados = cur.fetchall()
            columnas = [desc[0] for desc in cur.description]
            df = pd.DataFrame(resultados, columns=columnas)
        return df
    except Exception as e:
        print(f"Error al consultar la base de datos: {e}")
        return None
    finally:
        if 'conn' in locals():
            conn.close()

def lambda_handler(event, context):
    intent_name = event['sessionState']['intent']['name']
    mensaje = ""
    if intent_name == "ReadUser":
        df_usuarios = consultar_usuarios(document_number=1193037017)
        if df_usuarios is not None and not df_usuarios.empty:
            usuarios = df_usuarios['full_name'].tolist()
            mensaje = "Usuarios encontrados: " + ", ".join(str(u) for u in usuarios)
        elif df_usuarios is not None:
            mensaje = "No se encontraron usuarios."
        else:
            mensaje = "Error al consultar la base de datos."
    else:
        mensaje = "Intención no soportada."

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
                "content": mensaje
            }
        ]
    }