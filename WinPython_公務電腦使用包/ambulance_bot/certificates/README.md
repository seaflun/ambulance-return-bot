# 一站通中繼憑證

`twca_ssl_2026.cer` 是公開的 TWCA SSL Certification Authority 中繼憑證（Base64 PEM 格式），不含私鑰。

- 來源：網站新憑證 AIA 指定的 TWCA 官方位置 <https://sslserver.twca.com.tw/cacert/Cyber_SSL_2026.crt>。
- 取得日期：2026-11-03。
- DER SHA-256：`5775b8011198c498af26961ccbf77dac14a127a221ebc14012ed80f8a20f35f8`。
- Subject：`CN=TWCA SSL Certification Authority,O=TAIWAN-CA,C=TW`。
- Issuer：`CN=TWCA CYBER Root CA G3,O=TAIWAN-CA,C=TW`。
- 有效期間：2026-10-30 至 2035-10-30（UTC）。

2026-11-03 實測一站通仍送出 Subject 含 `OU=SSL Sub-CA` 的舊中繼憑證，
與新網站憑證的 Issuer 不相符。補上本檔案後，以既有信任根、正常主機名稱及 TLS 驗證連線成功。

程式只在一站通出現 `ERR_CERT_AUTHORITY_INVALID` 時，比對固定 DER 指紋，
將此中繼憑證加入 Windows **目前使用者的 CA 存放區**，並重新開啟登入頁一次。
不新增根信任、不關閉 Chrome 的憑證驗證；仍有警告即停止，未輸入帳密。
