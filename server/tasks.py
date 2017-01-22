import os
import random
import time
import subprocess
import logging
import zmq
import redis
import configfile
from start_celery import app
try:
    import Queue as queue # version < 3.0
except ImportError:
    import queue

# Create global context
context = zmq.Context()


def logmsg(msg):
    logging.basicConfig(format='%(asctime)s Message: %(message)s',
                        datefmt='%m/%d/%Y %I:%M:%S %p')
    logging.warning(msg)

    

@app.task
def mpd_trans(pathname):
    """
    Get a list of boxes from redis, then insert box's IP PORT to MPD file.
    At the same time, push it to queue for worker to execute send_media_to_box
    """
    # Connect to redis in order to get box's queryset
    rdb = redis.StrictRedis()
    random.seed(int(time.time()))
    
    # GET a list of box, try 5 times , if there's no avaiable
    # box then log error and return
    test = 5
    while test > 0:
        box_list = rdb.keys("box*")
        if len(box_list) > 0:
            break
        time.sleep(1)
        test -= 1
        
    if len(box_list) == 0:
        logmsg("mpd_trans: No box in list")
        return
    
    box = random.choice(box_list)
    
    ip_s, port_s = rdb.hmget(box, "IP", "PORT")
    ip = "http://"+ip_s+":"+port_s+"/"
    ip_mpd = "http://"+ip_s+":"+"8000"+"/"
    
    # dir_name / stream_name / basename = XXX.mpd,
    path, basename = pathname.rsplit('/', 1)
    dir_name, stream_name = path.rsplit('/', 1)
    
    # Open read file
    try:
        infile = open(pathname, "r")
    except IOError:
        logmsg(' IOError in mpd_trans() Pathname: %s.'%(pathname))
        return
    
    # Mpd output path
    output_folder = os.path.join(configfile.MPD_WRITE_DIR, stream_name)
    output_dir = os.path.join(output_folder, basename)
    # Check if the dir is created
    if not os.path.isdir(output_folder):
        subprocess.check_output(['mkdir', '-p', output_folder])
 
    # Open output file
    if not os.path.exists(output_dir):
        outfile = open(output_dir, "w")
        outfile.close()
    outfile = open(output_dir, "r+")
    outfile.seek(0)
    
    line = infile.readline()
    part = 1
    
    while line:
        if "<S t=" in line:
            pre_str, time_name, post_str = line.split("\"", 2)
            outfile.write(line)
    
            if part == 1 and not rdb.sismember(path, time_name):
                print path, " ", time_name
                m4v_path = path+"/"+time_name+".m4v"
                m4a_path = path+"/"+time_name+".m4a"
                send_media_to_box.delay(box, m4v_path)
                send_media_to_box.delay(box, m4a_path)
                rdb.sadd(path, time_name)
            
        elif "media" in line:
            pre_str, post_str = line.split("\"", 1)
            concentrate_str = (pre_str + "\"" + ip_mpd +
                               configfile.MPD_GET_DIR + "/" +
                               stream_name + "/" + post_str)
            outfile.write(concentrate_str+'\n')
            
        elif "initialization" in line:
            pre_str, post_str = line.split("\"", 1)
            #  media="ip/dash/output/stream_name/init.m4a"
            concentrate_str = (pre_str + "\"" + ip_mpd +
                               configfile.MPD_GET_DIR + "/" +stream_name +
                               "/" + post_str)
            outfile.write(concentrate_str+'\n')

            if part == 1:
                media_path = path + "/init.m4v"
            else:
                media_path = path + "/init.m4a"
            send_media_to_box.delay(box, media_path)
            
        elif "audio/mp4" in line:
            part = 2
            outfile.write(line)
            
        else:
            outfile.write(line)
        line = infile.readline()
        # End while
    # Close file
    rdb.expire(path, 200)
    outfile.truncate()
    infile.close()
    outfile.close()



def box_generator(rdb, stream_path, box_amount=10):
    """
    - Use Redis sorted set to achieve load balance among box
    - Get next available box by next() 
    """
    random.seed(int(time.time()))
    # GET a list of box, try 5 times , if there's no avaiable
    # box then log error and return
    for test in range(5):
        box_list = rdb.keys("box*")
        if len(box_list) > 0:
            break
        time.sleep(1)    
    if len(box_list) == 0:
        raise Exception("box_generator: No Box Exists in Redis")
    stream_set = rdb.zrange(stream_path, 0, -1)
    while len(box_list) > 0 and box_amount > 0:
        box = random.choice(box_list)
        if not stream_set or not box in stream_set:
            rdb.zadd(stream_path, 0, box)
        box_list.remove(box)
        box_amount-=1
    while True:
        box = rdb.zrangebyscore(stream_path, 0, '+inf', start=0, num=1)[0]
        if not rdb.exists(box):
            rdb.zrem(stream_path, box)  # Remove box if not exists anymore
            continue
        elif not box:
            raise Exception("box_generator: No available box.")
        ip, port = rdb.hmget(box, "IP", "PORT")
        rdb.zincrby(stream_path, box)    # Increment box's score by 1
        yield box, ip, port

def assign_box_to_media(rdb, generator, media_path):
    get_media = None
    if rdb.exists(media_path):
        get_media = rdb.hmget(media_path, "IP", "PORT", "CHECK")
    if get_media == None or get_media[2] == 'False':
        try:
            box_id, ip_s, port_s = generator.next()
            send_media_to_box.delay(box_id, media_path)
            if not get_media or (not get_media[0] == ip_s and not get_media[1] == port_s):
                rdb.hmset(media_path, {"IP":ip_s, "PORT":port_s, "CHECK":"False",})
            rdb.expire(media_path, 60)
            return None
        except Exception as e:
            logmsg(str(e))
            return None
    elif get_media[2] == 'True':
        ip_s, port_s, check = get_media
        return [ip_s, port_s]
    else:
        raise Exception("assign_box_to_media: Something goes wrong %s."%(media_path))
    
@app.task
def m3u8_trans(pathname):
    """
    - Read line from m3u8
    - Assign each media segment to different boxes
    """
    # Import from configfile
    M3U8_WRITE_DIR      = configfile.M3U8_WRITE_DIR
    M3U8_GET_DIR        = configfile.M3U8_GET_DIR
    GET_BOX_AMOUNT      = configfile.GET_BOX_AMOUNT
    M3U8_MEDIA_AMOUNT   = configfile.M3U8_MEDIA_AMOUNT
    SERVER_IP           = configfile.SERVER_IP
    SERVER_PORT         = configfile.SERVER_PORT
    # Connect to redis 
    rdb = redis.StrictRedis()
    # dir_name / stream_name / basename = XXX.mpd,
    path, basename = pathname.rsplit('/', 1)
    dir_name, stream_name = path.rsplit('/', 1)
    # Open read file
    try:
        infile = open(pathname, "r")
    except IOError:
        logmsg(' IOError in m3u8_trans() Pathname: %s.'%(pathname))
        return
    # m3u8 output path
    output_folder = os.path.join(M3U8_WRITE_DIR, stream_name)
    output_dir = os.path.join(output_folder, basename)
    # Check if the dir is created
    if not os.path.isdir(output_folder):
        subprocess.check_output(['mkdir', '-p', output_folder])
    # Open output file
    if not os.path.exists(output_dir):
        outfile = open(output_dir, "w")
        outfile.close()
    outfile = open(output_dir, "r+")
    outfile.seek(0)
    try:
        generator = box_generator(rdb, path, GET_BOX_AMOUNT)
    except Exception as e:
        logmsg(str(e))
        return
    line = infile.readline()
    first_media = True
    while line:
        if '.ts' == line.rstrip()[-3:]:
            # Send consecutive media to box in advance
            if first_media:
                head_time = int(line.split('.')[0])
                for timeline in range(head_time, head_time+M3U8_MEDIA_AMOUNT):
                    media_path = path + '/' + str(timeline) + '.ts'
                    try:
                        assign_box_to_media(rdb, generator, media_path)
                    except Exception as e:
                        print(str(e))
                first_media = False
            media_path = path + '/' + line.rstrip()
            ip_port = None
            # Try 5 times if no box can be assigned, then use server ip port
            for i in range(5):
                try:
                    ip_port = assign_box_to_media(rdb, generator, media_path)
                except Exception as e:
                        print(str(e))
                if not ip_port:
                    print "Try media path: %s again."%(media_path)
                    time.sleep(0.1)
                else:
                    break
                if i == 4:
                    rdb.hmset(media_path, {"IP":SERVER_IP, "PORT":SERVER_PORT, "CHECK":"True"})
                    rdb.expire(media_path, 60)
                    ip_port = [SERVER_IP, SERVER_PORT]
            # Modify current line
            ip_s, port_s = ip_port
            get_url_prefix = "http://"+ip_s+":"+port_s+"/"
            line = get_url_prefix + M3U8_GET_DIR + stream_name + "/" + line
            # end if
        outfile.write(line)
        line = infile.readline()
    infile.close()
    outfile.truncate()
    outfile.close()
    rdb.expire(path, 30)
    
      
@app.task
def send_media_to_box(box_id, media_path):
    """
    Send media tasks to boxes via proxy server
    """
    if not os.path.isfile(media_path):
        logmsg("send_media_to_box: %s file not found"%(media_path))
        return
    global context
    socket = context.socket(zmq.PUB)
    socket.connect(configfile.ZMQ_XSUB_ADDRESS)
    time.sleep(0.05)
    box_id = str(box_id)
    media_path = str(media_path)
    print media_path
    try:
        infile = open(media_path, "rb")
    except:
        logmsg("send_media_to_box can't open %s" % (media_path))
        return
    # Use BOX_ID as TOPIC
    data = [box_id, media_path]
    data.append(infile.read())
    # Send data
    socket.send_multipart(data)
    infile.close()
    socket.close()
    
