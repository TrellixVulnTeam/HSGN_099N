cl run --request-docker-image naster94/exo --request-network 'apt-get update && apt install git -y; git clone https://github.com/HaritzPuerto/HSGN ; mkdir HSGN/data; mkdir HSGN/data/external'  -n repo 