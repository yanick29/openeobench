import sys
import openeo

con = openeo.connect("https://openeo.dataspace.copernicus.eu/openeo/1.2")
con.authenticate_oidc()   # nutzt den gespeicherten Refresh Token

job_ids = sys.argv[1:] or ["j-2607100943004149887748aacd9b39a8"]

for jid in job_ids:
    job = con.job(jid)
    print(f"\n===== {jid} =====")
    print("Status:", job.status())
    for e in job.logs():
        print(f"[{e['level']:7}] {e['message']}")
