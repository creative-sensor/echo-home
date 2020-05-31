datum = ./data

datum_dir:
	mkdir -p ${datum}


setup:
	pip show qrcode &> /dev/null || pip install qrcode --user


svg: datum_dir
	eog $$(./qr-encoder.py)

