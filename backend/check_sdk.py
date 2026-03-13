import lark_oapi.api.im.v1 as im
names = [x for x in dir(im) if "image" in x.lower()]
print(names)
