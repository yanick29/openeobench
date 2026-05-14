import time
import rasterio
from rasterio.warp import calculate_default_transform, reproject, Resampling

input_file = "outputs/dem_download/openEO_2011-01-06Z.tif"
output_file = "outputs/dem_download/dem_reprojected_32633.tif"
dst_crs = "EPSG:32633"

start = time.time()

with rasterio.open(input_file) as src:
    print(f"Input CRS: {src.crs}")
    print(f"Input Shape: {src.shape}")
    
    transform, width, height = calculate_default_transform(
        src.crs, dst_crs, src.width, src.height, *src.bounds
    )
    
    kwargs = src.meta.copy()
    kwargs.update({
        'crs': dst_crs,
        'transform': transform,
        'width': width,
        'height': height
    })
    
    with rasterio.open(output_file, 'w', **kwargs) as dst:
        for i in range(1, src.count + 1):
            reproject(
                source=rasterio.band(src, i),
                destination=rasterio.band(dst, i),
                src_transform=src.transform,
                src_crs=src.crs,
                dst_transform=transform,
                dst_crs=dst_crs,
                resampling=Resampling.bilinear
            )

elapsed = time.time() - start
print(f"Output: {output_file}")
print(f"Resampling: bilinear")
print(f"Reprojection time: {elapsed:.2f} seconds")