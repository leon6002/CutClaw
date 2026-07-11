/** WGS84→GCJ02(与后端 src/utils/amap_geo.py 同公式)。
 *  高德瓦片/静态图都是火星坐标,照片 GPS 不转换会漂几百米。 */
export function wgs2gcj(lat: number, lon: number): [number, number] {
  if (!(lat >= 0.8293 && lat <= 55.8271 && lon >= 72.004 && lon <= 137.8347)) return [lat, lon];
  const a = 6378245.0, ee = 0.00669342162296594323, PI = Math.PI;
  const t = (x: number, y: number, m: "lat" | "lon") => {
    let r = m === "lat"
      ? -100 + 2 * x + 3 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * Math.sqrt(Math.abs(x))
      : 300 + x + 2 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * Math.sqrt(Math.abs(x));
    r += (20 * Math.sin(6 * x * PI) + 20 * Math.sin(2 * x * PI)) * 2 / 3;
    if (m === "lat") {
      r += (20 * Math.sin(y * PI) + 40 * Math.sin(y / 3 * PI)) * 2 / 3;
      r += (160 * Math.sin(y / 12 * PI) + 320 * Math.sin(y * PI / 30)) * 2 / 3;
    } else {
      r += (20 * Math.sin(x * PI) + 40 * Math.sin(x / 3 * PI)) * 2 / 3;
      r += (150 * Math.sin(x / 12 * PI) + 300 * Math.sin(x / 30 * PI)) * 2 / 3;
    }
    return r;
  };
  const dlat0 = t(lon - 105, lat - 35, "lat"), dlon0 = t(lon - 105, lat - 35, "lon");
  const rl = lat / 180 * PI, magic = 1 - ee * Math.sin(rl) ** 2, sm = Math.sqrt(magic);
  return [lat + (dlat0 * 180) / ((a * (1 - ee)) / (magic * sm) * PI),
          lon + (dlon0 * 180) / (a / sm * Math.cos(rl) * PI)];
}
