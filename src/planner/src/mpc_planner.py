#!/usr/bin/env python
"""
    File name: Online Planner-LPV-MPC.py
    Author: Eugenio Alcala
    Email: eugenio.alcala@upc.edu.edu
    Date: 25/07/2019
    Python Version: 2.7.12
"""

import os
import sys
import sys
sys.path.append(('/').join(sys.path[0].split('/')[:-2])+'/planner/src/')
sys.path.append(sys.path[0]+'/Utilities')
import datetime
import rospy
from trackInitialization import Map
from planner.msg import My_Planning
from sensor_fusion.msg import sensorReading
from controller.msg import  Racing_Info, states_info
import time
import numpy as np
from numpy import hstack
import scipy.io as sio
import pdb
import pickle
from utilities import Regression, Curvature
from dataStructures import LMPCprediction
# EstimatorData
# from osqp_formulation import LPV_MPC_Planner
from osqp_pathplanner import Path_planner_MPC

import matplotlib.pyplot as plt
import matplotlib.patches as patches
from scipy.interpolate import interp1d
from scipy import signal
from math import pi, isnan

np.set_printoptions(formatter={'float': lambda x: "{0:0.3f}".format(x)})


########## current state estimation from observer #############
class EstimatorData(object):
    """Data from estimator"""
    def __init__(self):

        print "waiting for estimator information"
        rospy.wait_for_message('est_state_info', sensorReading)

        rospy.Subscriber("est_state_info", sensorReading, self.estimator_callback)
        self.CurrentState = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        print "Subscribed to observer"
    
        self.map             = Map() 
        self.states_X =0
        self.states_Y =0
        self.states_yaw =0
        self.states_vx =0
        self.states_vy =0
        self.states_omega =0
        
        self.states_ey =0
        self.states_epsi =0
        self.states_s =0

        self.states = np.array([self.states_vx, self.states_vy, self.states_omega, 0, 0]).T


    def estimator_callback(self, msg):
        """
        Unpack the messages from the estimator
        """
        self.states_X = msg.X
        self.states_Y = msg.Y
        self.states_yaw = (msg.yaw + pi) % (2 * pi) - pi
        self.states_vx = msg.vx
        self.states_vy = msg.vy
        self.states_omega = msg.yaw_rate

        S_realDist, ey, epsi, insideTrack = self.map.getLocalPosition(msg.X, msg.Y, self.states_yaw)

        self.states_ey = ey
        self.states_epsi = epsi
        self.states_s = S_realDist


        self.CurrentState = np.array([msg.vx, msg.vy, msg.yaw_rate, msg.X, msg.Y, self.states_yaw]).T


        self.states = np.array([self.states_vx, self.states_vy, self.states_omega, ey, epsi]).T


########## current state of the vehicle from controller #############
class vehicle_state(object):
    """Data from estimator"""
    def __init__(self):


        rospy.Subscriber('control/states_info', states_info, self.states_callback)
        self.map             = Map() 
        self.states_X =0
        self.states_Y =0
        self.states_yaw =0
        self.states_vx =0
        self.states_vy =0
        self.states_omega =0
        self.states_ey =0
        self.states_epsi =0
        self.states_s =0
        self.states = np.array([self.states_vx, self.states_vy, self.states_omega, self.states_ey, self.states_epsi]).T
        print "Subscribed to observer"
    
    def states_callback(self, msg):
        """
        Unpack the messages from the estimator
        """
        self.states_X = msg.X
        self.states_Y = msg.Y
        self.states_yaw = msg.yaw
        self.states_vx = msg.vx
        self.states_vy = msg.vy
        self.states_omega = msg.omega
        self.states_ey = msg.ey
        self.states_epsi = msg.epsi
        self.states_s = msg.s
        
        S_realDist, ey, epsi, insideTrack = self.map.getLocalPosition(msg.X, msg.Y, wrap(msg.yaw))

        # self.states = np.array([self.states_vx, self.states_vy, self.states_omega, self.states_ey, self.states_epsi]).T
        self.states = np.array([self.states_vx, self.states_vy, self.states_omega, ey, epsi]).T



### wrap the angle between [-pi,pi] ###
def wrap(angle):
    if angle < -np.pi:
        w_angle = 2 * np.pi + angle
    elif angle > np.pi:
        w_angle = angle - 2 * np.pi
    else:
        w_angle = angle

    return w_angle


def main():

    # Initializa ROS node
    rospy.init_node("MPC_Planner")

    planning_refs   = rospy.Publisher('My_Planning', My_Planning, queue_size=1)

    map             = Map()  

    refs            = My_Planning()

    refs.x_d        = []
    refs.y_d        = []
    refs.psi_d      = []
    refs.vx_d       = []
    refs.curv_d     = [] 

    HW              = rospy.get_param("/trajectory_planner/halfWidth")
    # HW              = 0.35 # It is a bit larger than the configured in the launch with the aim of improving results
    loop_rate       = rospy.get_param("/trajectory_planner/Hz") # 20 Hz (50 ms)
    dt              = 1.0/loop_rate
    rate            = rospy.Rate(loop_rate)

    # curr_states = EstimatorData()
    
    planner_type = rospy.get_param("/trajectory_planner/Mode")
    if planner_type == 2:
        racing_info     = RacingDataClass()
        estimatorData   = EstimatorData()

    if planner_type == 3:
        racing_info     = RacingDataClass()
        estimatorData   = EstimatorData()
        mode            = rospy.get_param("/control/mode")


    else:   # planner_type mode
        mode            = "simulations"
        racing_info     = RacingDataClass()


    first_it        = 1

    Xlast           = 0.0
    Ylast           = 0.0
    Thetalast       = 0.0

    Counter         = 0

    ALL_LOCAL_DATA  = np.zeros((1000,7))       # [vx vy psidot ey psi udelta uaccel]
    References      = np.zeros((1000,5))

    ELAPSD_TIME     = np.zeros((1000,1))

    TimeCounter     = 0
    PlannerCounter  = 0
    start_LapTimer  = datetime.datetime.now()


    if rospy.get_param("/trajectory_planner/Visualization") == 1:
        fig, axtr, line_tr, line_pred, line_trs, line_cl, line_gps_cl, rec, rec_sim = InitFigure_XY( map, mode, HW )


    #####################################################################

    N   = rospy.get_param("/trajectory_planner/N")

    planner_dt = dt
    # planner_dt = 0.05
    Vx_ref              = rospy.get_param("/trajectory_planner/vel_ref")

    Planner  = Path_planner_MPC(N, Vx_ref, dt, map)

    # Planner  = LPV_MPC_Planner(Q, R, dR, L, N, planner_dt, map, "OSQP")

    #####################################################################

    # Filter to be applied.
    b_filter, a_filter = signal.ellip(4, 0.01, 120, 0.125)  


    LPV_X_Pred      = np.zeros((N,5))
    Xref            = np.zeros(N+1)
    Yref            = np.zeros(N+1)
    Thetaref        = np.zeros(N+1)
    xp              = np.zeros(N)
    yp              = np.zeros(N)
    yaw             = np.zeros(N)    

    x_his          = []
    y_his          = []
    SS              = np.zeros(N+1,) 

    GlobalState     = np.zeros(6)
    LocalState      = np.zeros(5)    

    print "Starting iteration"
    while (not rospy.is_shutdown()):  
        


        startTimer = datetime.datetime.now()

        ###################################################################################################
        # GETTING INITIAL STATE:
        ###################################################################################################   

        if planner_type == 2:
            # Read Measurements
            # GlobalState[:] = estimatorData.CurrentState                 # The current estimated state vector [vx vy w x y psi]
            # LocalState[:]  = np.array([ GlobalState[0], GlobalState[1], GlobalState[2], 0.0, 0.0 ]) # [vx vy w ey epsi]
            # S_realDist, LocalState[4], LocalState[3], insideTrack = map.getLocalPosition(GlobalState[3], GlobalState[4], GlobalState[5])
            LocalState[:] = estimatorData.states                 # The current estimated state vector [vx vy w x y psi]
            # 

        # else:
        #     LocalState[:] = np.array([1.0, 0, 0, 0, 0])
        #     S_realDist, LocalState[4], LocalState[3], insideTrack = map.getLocalPosition(xp[0], yp[0], yaw[0])
            
        ###################################################################################################
        # OPTIMIZATION:
        ###################################################################################################             
        # if Counter == 5:
        #     break



        if first_it < 2:
            # Resolvemos la primera OP con los datos del planner no-lineal:
            # xx son los estados del modelo no lineal para la primera it.
            # delta es el steering angle del modelo no lineal para la primera it.

        
            x0 = LocalState[:]        # Initial planning state
            print "x0",x0
            duty_cycle = 0.0
            delta = 0.0
            xx, uu = predicted_vectors_generation(N, x0, duty_cycle, delta, planner_dt)
           
            # print "uu", uu
            Planner.uPred = uu
            Planner.xPred = xx[:,:5]

            print "xx[:5]", xx[:,:5]
            Planner.uminus1 = Planner.uPred[0,:] 


            

            first_it += 1



        else:                               

            t0 = time.time()

            print "\n solve new"

            if first_it == 2:
            
                print "MPC setup"

                # LPV_X_Pred, A_L, B_L, C_L = Planner.LPVPrediction_setup()
                A_L, B_L, C_L = Planner.LPVPrediction_setup()

                Planner.MPC_setup(A_L, B_L, Planner.uPred, Planner.xPred[1,:], Vx_ref) 

                first_it += 1



            print "MPC update"
            t1 = time.time() 

            if planner_type == 1:
                SS[0] = SS[1]
                # LPV_X_Pred, A_L, B_L, C_L = Planner.LPVPrediction( Planner.xPred[1,:], SS[:] ,  Planner.uPred )    
                A_L, B_L, C_L = Planner.LPVPrediction( Planner.xPred[1,:], SS[:] ,  Planner.uPred )    
            
                Planner.MPC_update(A_L, B_L, Planner.xPred[1,:]) 
                Planner.MPC_solve()


            if planner_type == 2:


                SS[0] = estimatorData.states_s

                A_L, B_L, C_L = Planner.LPVPrediction( estimatorData.states  , SS[:] , Planner.uPred )    
                
                Planner.MPC_update(A_L, B_L, estimatorData.states  ) 
                Planner.MPC_solve()
               
            print "time taken to solve", time.time() - t1
            # print "LPV_X_Pred", LPV_X_Pred                



            # Planner.solve(curr_states.states, 0, 0, A_L, B_L, C_L, first_it, HW)

            print "time taken", time.time() - t0
        print "feasible", Planner.feasible
        # print "predicted", Planner.xPred
        # print "Planner.uPred ", Planner.uPred
        Planner.uminus1 = Planner.uPred[0,:] 

        if Planner.feasible == 0:

            Planner.uPred   = np.zeros((Planner.N, Planner.nu))
            Planner.uminus1 = Planner.uPred[0,:]


        if Planner.feasible != 0:
            ###################################################################################################
            ###################################################################################################

            # print "Ey: ", Planner.xPred[:,3] 

            # Saving current control actions to perform then the slew rate:
            # Planner.OldSteering.append(Planner.uPred[0,0]) 
            # Planner.OldAccelera.append(Planner.uPred[0,1])

            # pdb.set_trace()

            #####################################
            ## Getting vehicle position:
            #####################################
            Xref[0]     = Xlast
            Yref[0]     = Ylast
            Thetaref[0] = Thetalast

            # print "SS[0] = ", S_realDist

            # SS[0] = S_realDist
            # SS  = np.zeros(N+1,) 

            for j in range( 0, N ):
                PointAndTangent = map.PointAndTangent         
                
                curv            = Curvature( SS[j], PointAndTangent )

                # print "Planner.xPred[j,0]", Planner.xPred[j,0], 'Planner.xPred[j,4]', Planner.xPred[j,4], 'Planner.xPred[j,3]', Planner.xPred[j,3] 
                SS[j+1] = ( SS[j] + ( ( Planner.xPred[j,0]* np.cos(Planner.xPred[j,4])
                 - Planner.xPred[j,1]*np.sin(Planner.xPred[j,4]) ) / ( 1-Planner.xPred[j,3]*curv ) ) * planner_dt ) 

                '''Only valid if the SS[j+1] value is close to 0'''
                if -0.001 < SS[j+1] < 0.001:
                    SS[j+1] = 0.0

                # print "SS", SS
                # print 'ssj+1', SS[j+1]
                # print "map.getGlobalPosition( SS[j+1], 0.0 )", map.getGlobalPosition( SS[j+1], 0.0 )
                # Xref[j+1], Yref[j+1], Thetaref[j+1] = map.getGlobalPosition( SS[j+1], curr_states.states_ey )
                Xref[j+1], Yref[j+1], Thetaref[j+1] = map.getGlobalPosition( SS[j+1], 0.0 )

            Xlast = Xref[1]
            Ylast = Yref[1]
            Thetalast = Thetaref[1]

            for i in range(0,N):
                yaw[i]  = Thetaref[i] + Planner.xPred[i,4]
                xp[i]   = Xref[i] - Planner.xPred[i,3]*np.sin(yaw[i])
                yp[i]   = Yref[i] + Planner.xPred[i,3]*np.cos(yaw[i])        

            vel     = Planner.xPred[0:N,0]     
            curv    = Planner.xPred[0:N,2] / Planner.xPred[0:N,0]  


            endTimer    = datetime.datetime.now()
            deltaTimer  = endTimer - startTimer

            ELAPSD_TIME[Counter,:]      = deltaTimer.total_seconds()



            #####################################
            ## Plotting vehicle position:
            #####################################     

            if rospy.get_param("/trajectory_planner/Visualization") == 1:
                line_trs.set_data(xp[0:N/2], yp[0:N/2])
                line_pred.set_data(xp[N/2:], yp[N/2:])
                x_his.append(xp[0])
                y_his.append(yp[0])
                line_cl.set_data(x_his, y_his)
                l = 0.4/2; w = 0.2/2
                car_sim_x, car_sim_y = getCarPosition(xp[0], yp[0], yaw[0], w, l)
                # car_sim_x, car_sim_y = getCarPosition(xp[N-1], yp[N-1], yaw[N-1], w, l)
                rec_sim.set_xy(np.array([car_sim_x, car_sim_y]).T)
                fig.canvas.draw()

                plt.show()
                plt.pause(1/2000.0)
                StringValue = "vx = "+str(Planner.xPred[0,0]) + " epsi =" + str(Planner.xPred[0,4]) 
                axtr.set_title(StringValue)


     


            #####################################
            ## Interpolating vehicle references:
            #####################################  
            interp_dt = 1.0/50
            time50ms = np.linspace(0, N*dt, num=N, endpoint=True)
            time33ms = np.linspace(0, N*dt, num=np.around(N*dt/interp_dt), endpoint=True)

            # X 
            f = interp1d(time50ms, xp, kind='cubic')
            X_interp = f(time33ms)  

            # Y
            f = interp1d(time50ms, yp, kind='cubic')
            Y_interp = f(time33ms)  

            # Yaw
            f = interp1d(time50ms, yaw, kind='cubic')
            Yaw_interp = f(time33ms)  

            # Velocity (Vx)
            f = interp1d(time50ms, vel, kind='cubic')
            Vx_interp = f(time33ms)

            # Curvature (K)
            f = interp1d(time50ms, curv, kind='cubic')
            Curv_interp = f(time33ms)     
            # Curv_interp_filtered  = signal.filtfilt(b_filter, a_filter, Curv_interp, padlen=50)

            # plt.clf()
            # plt.figure(2)
            # plt.plot(Curv_interp, 'k-', label='input')
            # plt.plot(Curv_interp_filtered,  'c-', linewidth=1.5, label='pad')
            # plt.legend(loc='best')
            # plt.show()
            # plt.grid()

            # pdb.set_trace()



            #####################################
            ## Publishing vehicle references:
            #####################################   

            # refs.x_d        = xp
            # refs.y_d        = yp
            # refs.psi_d      = yaw
            # refs.vx_d       = vel 
            # refs.curv_d     = curv 
            refs.x_d        = X_interp
            refs.y_d        = Y_interp
            refs.psi_d      = Yaw_interp
            refs.vx_d       = Vx_interp 
            refs.curv_d     = Curv_interp#_filtered
            print "Vx_interp size", len(Vx_interp)             
            planning_refs.publish(refs)


        # ALL_LOCAL_DATA[Counter,:]   = np.hstack(( Planner.xPred[0,:], Planner.uPred[0,:] ))
        # References[Counter,:]       = np.hstack(( refs.x_d[0], refs.y_d[0], refs.psi_d[0], refs.vx_d[0], refs.curv_d[0] ))


        # Increase time counter and ROS sleep()
        TimeCounter     += 1
        PlannerCounter  += 1
        Counter         += 1


        rate.sleep()




    #############################################################
    # day         = '30_7_19'
    # num_test    = 'References'

    # newpath = ('/').join(__file__.split('/')[:-2]) + '/data/'+day+'/'+num_test+'/' 
    # if not os.path.exists(newpath):
    #     os.makedirs(newpath)

    # np.savetxt(newpath+'/References.dat', References, fmt='%.5e')


    # #############################################################
    # # day         = '29_7_19'
    # # num_test    = 'Test_1'
    # newpath = ('/').join(__file__.split('/')[:-2]) + '/data/'+day+'/'+num_test+'/' 

    # if not os.path.exists(newpath):
    #     os.makedirs(newpath)

    # np.savetxt(newpath+'/ALL_LOCAL_DATA.dat', ALL_LOCAL_DATA, fmt='%.5e')
    # np.savetxt(newpath+'/PREDICTED_DATA.dat', PREDICTED_DATA, fmt='%.5e')
    # np.savetxt(newpath+'/GLOBAL_DATA.dat', GLOBAL_DATA, fmt='%.5e')
    # np.savetxt(newpath+'/Complete_Vel_Vect.dat', Complete_Vel_Vect, fmt='%.5e')
    # np.savetxt(newpath+'/References.dat', References, fmt='%.5e')
    # np.savetxt(newpath+'/TLAPTIME.dat', TLAPTIME, fmt='%.5e')
    # np.savetxt(newpath+'/ELAPSD_TIME.dat', ELAPSD_TIME, fmt='%.5e')


    plt.close()

    # time50ms = np.linspace(0, (Counter-1)*dt, num=Counter-1, endpoint=True)
    # time33ms = np.linspace(0, (Counter-1)*dt, num=np.around((Counter-1)*dt/0.033), endpoint=True)
    # f = interp1d(time50ms, References[0:Counter-1,3], kind='cubic')

    # plt.figure(2)
    # plt.subplot(211)
    # plt.plot(References[0:Counter-1,3], 'o')
    # plt.legend(['Velocity'], loc='best')
    # plt.grid()
    # plt.subplot(212)
    # # plt.plot(time33ms, f(time33ms), 'o')
    # # plt.legend(['Velocity interpolated'], loc='best')    
    # plt.plot(References[0:Counter-1,4], '-')
    # plt.legend(['Curvature'], loc='best')    
    # plt.show()
    # plt.grid()
    # pdb.set_trace()


    quit() # final del while






# ==========================================================================================================================
# ==========================================================================================================================
# ==========================================================================================================================
# ==========================================================================================================================

class RacingDataClass(object):
    """ Object collecting data from racing performance """

    def __init__(self):

        rospy.Subscriber('Racing_Info', Racing_Info, self.racing_info_callback, queue_size=1)

        self.LapNumber          = 0
        self.PlannerCounter     = 0

    def racing_info_callback(self,data):
        """ ... """      
        self.LapNumber          = data.LapNumber
        self.PlannerCounter     = data.PlannerCounter




# ===============================================================================================================================
# ==================================================== END OF MAIN ==============================================================
# ===============================================================================================================================

def InitFigure_XY(map, mode, HW):
    xdata = []; ydata = []
    fig = plt.figure(figsize=(10,8))
    plt.ion()
    axtr = plt.axes()

    Points = int(np.floor(10 * (map.PointAndTangent[-1, 3] + map.PointAndTangent[-1, 4])))
    Points1 = np.zeros((Points, 3))
    Points2 = np.zeros((Points, 3))
    Points0 = np.zeros((Points, 3))

    for i in range(0, int(Points)):
        Points1[i, :] = map.getGlobalPosition(i * 0.1, HW)
        Points2[i, :] = map.getGlobalPosition(i * 0.1, -HW)
        Points0[i, :] = map.getGlobalPosition(i * 0.1, 0)

    plt.plot(map.PointAndTangent[:, 0], map.PointAndTangent[:, 1], 'o')
    plt.plot(Points0[:, 0], Points0[:, 1], '--')
    plt.plot(Points1[:, 0], Points1[:, 1], '-b')
    plt.plot(Points2[:, 0], Points2[:, 1], '-b')


    line_cl,        = axtr.plot(xdata, ydata, '-k')
    line_gps_cl,    = axtr.plot(xdata, ydata, '--ob')
    line_tr,        = axtr.plot(xdata, ydata, '-or')
    line_trs,       = axtr.plot(xdata, ydata, '-og')
    line_pred,      = axtr.plot(xdata, ydata, '-or')
    
    v = np.array([[ 1.,  1.],
                  [ 1., -1.],
                  [-1., -1.],
                  [-1.,  1.]])

    rec = patches.Polygon(v, alpha=0.7,closed=True, fc='r', ec='k',zorder=10)
    # axtr.add_patch(rec)

    rec_sim = patches.Polygon(v, alpha=0.7,closed=True, fc='G', ec='k',zorder=10)

    if mode == "simulations":
        axtr.add_patch(rec_sim)

    plt.show()

    return fig, axtr, line_tr, line_pred, line_trs, line_cl, line_gps_cl, rec, rec_sim



def getCarPosition(x, y, psi, w, l):
    car_x = [ x + l * np.cos(psi) - w * np.sin(psi), x + l*np.cos(psi) + w * np.sin(psi),
              x - l * np.cos(psi) + w * np.sin(psi), x - l * np.cos(psi) - w * np.sin(psi)]
    car_y = [ y + l * np.sin(psi) + w * np.cos(psi), y + l * np.sin(psi) - w * np.cos(psi),
              y - l * np.sin(psi) - w * np.cos(psi), y - l * np.sin(psi) + w * np.cos(psi)]
    return car_x, car_y



def predicted_vectors_generation(Hp, x0, accel_rate, delta, dt):

    Vx      = np.zeros((Hp+1, 1))
    Vx[0]   = x0[0]
    S       = np.zeros((Hp+1, 1))
    S[0]    = 0
    Vy      = np.zeros((Hp+1, 1))
    Vy[0]   = x0[1]
    W       = np.zeros((Hp+1, 1))
    W[0]    = x0[2]
    Ey      = np.zeros((Hp+1, 1))
    Ey[0]   = x0[3]
    Epsi    = np.zeros((Hp+1, 1))
    Epsi[0] = x0[4]

    curv    = 0

    for i in range(0, Hp): 
        Vy[i+1]      = x0[1]  
        W[i+1]       = x0[2] 
        Ey[i+1]      = x0[3] 
        Epsi[i+1]    = x0[4] 

    accel   = np.array([[accel_rate for i in range(0, Hp)]])
    delta   = np.array([[ delta for i in range(0, Hp)]])

    for i in range(0, Hp):
        Vx[i+1]    = Vx[i] + accel[0,i] * dt
        S[i+1]      = S[i] + ( (Vx[i]*np.cos(Epsi[i]) - Vy[i]*np.sin(Epsi[i])) / (1-Ey[i]*curv) ) * dt

    # print "Vx = ", Vx
    # print "Vy = ", np.transpose(Vy)
    # print "W = ", W

    # pdb.set_trace()

    xx  = np.hstack([ Vx, Vy, W, Ey, Epsi, S])    

    uu  = np.hstack([delta.transpose(),accel.transpose()])

    return xx, uu





if __name__ == "__main__":

    try:    
        main()
        
    except rospy.ROSInterruptException:
        pass
