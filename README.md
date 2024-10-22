# configConverter

Utilizes several tools to convert switch configurations to Arista EOS
- Arista AVD (https://avd.sh/en/stable/)
- The amazing config parser library from (https://github.com/tdorssers/confparser.git) included here with a single modification to output an alert for non-matching lines

Note: This tool has purposefully been written using a simplistic design to aid in readability and extensibility.  While this may introduce inefficiencies in processing, it does allow for easier grokking by non-developers.

## Requirements
`pip install -r requirements.txt`

## usage
```
$ python configConverter.py --help
usage: configConverter.py [-h] -i I [--dissector DISSECTOR] [--output OUTPUT]

options:
  -h, --help            show this help message and exit
  -i I                  input file
  --dissector DISSECTOR
                        dissector file. default=ios.yaml
  --output OUTPUT       default=text, text|yaml
```
