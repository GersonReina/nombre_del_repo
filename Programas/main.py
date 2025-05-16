import psycopg2
import pandas as pd

conn = psycopg2.connect(
    host = 'dataanalytics-dhw.cw69ipuk8wdc.us-east-1.redshift.amazonaws.com',
    port = 5439,
    dbname = 'dev',
    user = 'admindata',
    password = 'Data4nalyticS**',
    options="-c client_encoding=UTF8"
) 
    
try:
    # Cursor para la consulta
    with conn.cursor() as cur:
        cur.execute("""
            select last_name, first_name, document_number 
            from my_bodytech.members m
            where last_name = 'Reina Ramos'
            limit 100;
        """)
        # Obtener los resultados y los nombres de las columnas
        resultados = cur.fetchall()
        columnas = [desc[0] for desc in cur.description]

        # Crear un DataFrame con los resultados
        df_fechpag = pd.DataFrame(resultados, columns=columnas)

        # Imprimir las primeras filas del DataFrame
        print(df_fechpag.head())

except Exception as e:
    print(f"Error al ejecutar la consulta: {e}")
finally:
    # Asegurar que la conexión se cierre
    conn.close()